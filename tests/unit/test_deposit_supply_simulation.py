"""FIN-001 integration and financial invariants for the isolated simulation."""

from dataclasses import replace
from itertools import product

import numpy as np

import pytest

from balance_pricing_engine.config import (
    DepositTierConfig,
    GridConfig,
    LoanTierConfig,
    SegmentedPricingConfig,
    load_config,
)
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.optimization.segmented import SolverSettings
from balance_pricing_engine.pricing.deposit_supply import (
    ConcentrationMode,
    DepositConcentrationPolicy,
    DepositMarketProfile,
    DepositOpportunitySegment,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.deposit_supply_simulation import (
    DepositSupplySimulationConfig,
    run_deposit_supply_simulation,
)
from balance_pricing_engine.scenarios.runner import (
    _generate_pricing_population,
    run_baseline_diagnostics,
)
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


@pytest.fixture
def inputs():
    config = load_config("configs/base.yaml")
    profile = DepositMarketProfile(
        profile_id="parity",
        version="1",
        opportunity_basis="FLOW",
        review_notes="Illustrative test only.",
        segments=(DepositOpportunitySegment(segment_id="external", opportunity_nzd=500e6),),
        concentration=DepositConcentrationPolicy(
            mode=ConcentrationMode.SOFT_PENALTY,
            limit_nzd=100e6,
            penalty_rate=0.015,
        ),
    )
    return DepositSupplySimulationConfig(
        engine_config=config, profile=profile, max_boundary_expansions=0
    )


def _grid(values):
    return GridConfig(start=values[0], stop=values[-1], step=values[1] - values[0])


def test_joint_and_segmented_catch_all_match(inputs):
    loan_rates, deposit_rates = build_configured_rate_grids(inputs.engine_config)
    segmented = inputs.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=_grid(loan_rates))],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=_grid(deposit_rates))],
            ),
        }
    )
    joint_result = run_deposit_supply_simulation(inputs)
    tier_result = run_deposit_supply_simulation(
        inputs.model_copy(update={"engine_config": segmented})
    )
    assert (
        tier_result.diagnostics.best_result.loan_rate
        == joint_result.diagnostics.best_result.loan_rate
    )
    assert (
        tier_result.diagnostics.best_result.deposit_rate
        == joint_result.diagnostics.best_result.deposit_rate
    )
    assert (
        tier_result.diagnostics.winner_decomposition()
        == joint_result.diagnostics.winner_decomposition()
    )
    assert tier_result.estimate == joint_result.estimate
    for field in (
        "selected_return_on_equity",
        "policy_band_breached",
        "roe_constraint_relaxed",
        "expansion_capped",
    ):
        assert getattr(tier_result.diagnostics, field) == getattr(joint_result.diagnostics, field)
    for field in ("boundary_diagnostics", "initial_boundary_diagnostics"):
        assert getattr(tier_result.diagnostics, field).has_warning == (
            getattr(joint_result.diagnostics, field).has_warning
        )
    assert tier_result.governance_status == joint_result.governance_status
    assert tier_result.diagnostics.segmented_solver["optimality_proven"] is True


def test_fixed_offer_reconciles_funding_and_cost(inputs):
    result = run_deposit_supply_simulation(
        inputs.model_copy(update={"fixed_rates": (0.0619, 0.0405)})
    )
    best = result.diagnostics.best_result
    estimate = result.estimate
    config = inputs.engine_config
    wholesale = config.market.cash_rate + config.market.wholesale_funding_spread
    assert estimate.usable_funding == pytest.approx(estimate.gross_bookings)
    assert best.expected_deposit_volume == pytest.approx(estimate.gross_bookings)
    assert best.deposit_interest_cost == pytest.approx(estimate.gross_bookings * 0.0405)
    assert best.wholesale_funding_cost == pytest.approx(
        max(best.expected_loan_volume - estimate.usable_funding, 0) * wholesale
    )
    assert best.funding_shortfall_penalty == pytest.approx(
        max(best.expected_loan_volume * 0.65 - estimate.usable_funding, 0) * 0.018
    )
    assert result.diagnostics.winner_decomposition().reconciled_total_objective == pytest.approx(
        best.total_objective
    )
    assert result.diagnostics.policy_band_breached is True
    assert result.concentration_comparison.objective_delta <= 0


def test_profile_lineage_and_canonical_isolation(inputs):
    baseline = run_baseline_diagnostics(inputs.engine_config)
    first = run_deposit_supply_simulation(inputs)
    assert first.estimate == run_deposit_supply_simulation(inputs).estimate
    assert first.governance_status is DecisionStatus.REVIEW_REQUIRED
    assert (
        first.config_hash
        != run_deposit_supply_simulation(
            inputs.model_copy(
                update={"profile": inputs.profile.model_copy(update={"review_notes": "different"})}
            )
        ).config_hash
    )
    first.effective_config.engine_config.market.cash_rate = 0.0
    inputs.engine_config.market.cash_rate = 0.01
    assert first.effective_config.engine_config.market.cash_rate != 0.01
    assert run_baseline_diagnostics(first.effective_config.engine_config).best_result == (
        baseline.best_result
    )


def test_block_governance_precedes_supply_review(inputs):
    result = run_deposit_supply_simulation(inputs)
    diagnostics = replace(
        result.diagnostics,
        best_result=replace(result.diagnostics.best_result, total_objective=float("nan")),
    )
    gates = evaluate_gates(
        config=inputs.engine_config, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    assert "DEPOSIT_SUPPLY_SIMULATION_ONLY" in {gate.code for gate in gates}
    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


def test_live_inputs_and_rolling_config_rejected(inputs):
    live = inputs.engine_config.competitor_rates.model_copy(update={"enabled": True})
    with pytest.raises(ValueError, match="static market"):
        inputs.model_copy(
            update={
                "engine_config": inputs.engine_config.model_copy(update={"competitor_rates": live})
            }
        )
    with pytest.raises(ValueError, match="single-horizon"):
        inputs.model_copy(
            update={"engine_config": inputs.engine_config.model_copy(update={"multiperiod": {}})}
        )


@pytest.mark.parametrize("force_best_known", [False, True])
def test_multitier_search_against_independent_enumeration(inputs, force_best_known, monkeypatch):
    import balance_pricing_engine.scenarios.runner as runner

    if force_best_known:
        monkeypatch.setattr(
            runner,
            "SEGMENTED_SOLVER_SETTINGS",
            SolverSettings(
                exact_search_max_combinations=1,
            ),
        )

    low_loan, high_loan = (0.058, 0.062), (0.060, 0.064)
    low_deposit, high_deposit = (0.037, 0.042), (0.038, 0.043)
    engine = inputs.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[
                    LoanTierConfig(name="low", lvr_max=0.8, rate_grid=_grid(low_loan)),
                    LoanTierConfig(name="high", rate_grid=_grid(high_loan)),
                ],
                deposit_tiers=[
                    DepositTierConfig(
                        name="low", balance_max=100_000, rate_grid=_grid(low_deposit)
                    ),
                    DepositTierConfig(name="high", rate_grid=_grid(high_deposit)),
                ],
            ),
        }
    )
    capacity = 550e6
    cap = 350e6
    acquisition_rate = 0.002
    segment = DepositOpportunitySegment(
        segment_id="external",
        opportunity_nzd=capacity,
        acquisition_cost_rate=acquisition_rate,
    )
    profile = inputs.profile.model_copy(
        update={
            "segments": (segment,),
            "concentration": DepositConcentrationPolicy(
                mode=ConcentrationMode.HARD_LIMIT,
                limit_nzd=cap,
            ),
        }
    )
    result = run_deposit_supply_simulation(
        inputs.model_copy(
            update={
                "engine_config": engine,
                "profile": profile,
            }
        )
    )

    loans, deposits = _generate_pricing_population(engine)
    loan_low = loans["lvr"] < 0.8
    deposit_low = deposits["deposit_amount"] < 100_000
    opportunity_weights = deposits["deposit_amount"] / deposits["deposit_amount"].sum() * capacity
    market, response, balance_sheet = engine.market, engine.response, engine.balance_sheet

    candidates = []
    for rates in product(low_loan, high_loan, low_deposit, high_deposit):
        loan_volume = loan_income = credit_loss = gross = deposit_cost = 0.0
        for rate, mask in zip(rates[:2], (loan_low, ~loan_low)):
            cohort = loans.loc[mask]
            probabilities = loan_acceptance_probability(
                cohort,
                rate,
                market.competitor_loan_rate,
                response.loan_rate_elasticity,
                response.loan_competitor_gap_elasticity,
                coefficients=response.coefficients.loan,
            )
            accepted_balances = probabilities * cohort["loan_amount"]
            loan_volume += accepted_balances.sum()
            loan_income += accepted_balances.sum() * rate
            credit_loss += (accepted_balances * cohort["expected_loss_rate"]).sum()
        for rate, mask in zip(rates[2:], (deposit_low, ~deposit_low)):
            probabilities = deposit_uptake_probability(
                deposits.loc[mask],
                rate,
                market.competitor_deposit_rate,
                response.deposit_rate_elasticity,
                response.deposit_competitor_gap_elasticity,
                coefficients=response.coefficients.deposit,
            )
            booked = np.sum(probabilities * opportunity_weights.loc[mask])
            gross += booked
            deposit_cost += booked * rate
        usable = min(gross, cap)
        capital = loan_volume * balance_sheet.loan_rwa_weight * 0.1
        objective = (
            loan_income
            - credit_loss
            - deposit_cost
            - gross * acquisition_rate
            - capital * balance_sheet.capital_hurdle_rate
            - max(loan_volume - usable, 0) * (market.cash_rate + market.wholesale_funding_spread)
            - max(loan_volume * balance_sheet.required_deposit_funding_ratio - usable, 0)
            * balance_sheet.funding_shortfall_penalty_rate
            + max(usable - loan_volume, 0) * market.cash_rate
        )
        candidates.append((objective, rates, gross, usable))
    tier_rates = result.diagnostics.segmented_tier_rates
    selected = tuple(
        tier_rates[side][tier]
        for side in ("loan_tiers", "deposit_tiers")
        for tier in ("low", "high")
    )
    actual = next(candidate for candidate in candidates if candidate[1] == selected)
    assert result.estimate.eligible_opportunity == capacity
    assert result.estimate.gross_bookings == pytest.approx(actual[2])
    assert result.estimate.usable_funding == pytest.approx(actual[3])
    assert result.diagnostics.best_result.total_objective == pytest.approx(actual[0])
    assert result.diagnostics.winner_decomposition().reconciled_total_objective == pytest.approx(
        actual[0]
    )
    assert result.diagnostics.segmented_solver["optimality_proven"] is not force_best_known
    if not force_best_known:
        assert actual == max(candidates)
    assert result.concentration_comparison.uncapped_tier_rates


def test_only_the_deposit_sample_changes_with_deposit_seed(inputs):
    first = run_deposit_supply_simulation(
        inputs.model_copy(
            update={
                "fixed_rates": (0.0619, 0.0405),
                "deposit_sample_seed": 10,
            }
        )
    )
    second = run_deposit_supply_simulation(
        first.effective_config.model_copy(
            update={
                "deposit_sample_seed": 11,
            }
        )
    )
    assert first.diagnostics.best_result.expected_loan_volume == (
        second.diagnostics.best_result.expected_loan_volume
    )
    assert first.estimate.gross_bookings != second.estimate.gross_bookings
    assert first.estimate.eligible_opportunity == second.estimate.eligible_opportunity
    assert first.config_hash != second.config_hash


def test_supply_artifacts_report_reconciled_costs_and_skip_legacy_cohort_metric(inputs, tmp_path):
    result = run_deposit_supply_simulation(inputs)
    result.diagnostics.write_artifacts(tmp_path)
    assert "deposit_concentration_penalty" in (tmp_path / "counterfactuals.csv").read_text()
    assert "deposit_supply_simulation" in (tmp_path / "summary.json").read_text()
    assert not (tmp_path / "cohort_attribution.csv").exists()


def test_nonfinite_simulated_roe_cannot_qualify_as_a_floor_pass(inputs):
    loan_grid = _grid((0.05, 2.0))
    deposit_grid = GridConfig(start=0.04, stop=0.04, step=0.005)
    engine = inputs.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "products": {"loan_rate_grid": loan_grid, "deposit_rate_grid": deposit_grid},
            "market": inputs.engine_config.market.model_copy(
                update={"home_loan_reference_rate": 2.0}
            ),
            "balance_sheet": inputs.engine_config.balance_sheet.model_copy(
                update={"min_return_on_equity": 0.05}
            ),
        }
    )
    profile = inputs.profile.model_copy(
        update={
            "opening_funding_nzd": 1e308,
            "segments": (DepositOpportunitySegment(segment_id="external", opportunity_nzd=0),),
            "concentration": None,
        }
    )
    inputs = inputs.model_copy(update={"engine_config": engine, "profile": profile})
    segmented = engine.model_copy(
        update={
            "products": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=loan_grid)],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=deposit_grid)],
            ),
        }
    )
    with np.errstate(over="ignore"):
        joint = run_deposit_supply_simulation(inputs).diagnostics
        tiered = run_deposit_supply_simulation(
            inputs.model_copy(update={"engine_config": segmented})
        ).diagnostics
    assert joint.best_result.loan_rate == tiered.best_result.loan_rate == 0.05
    assert joint.roe_constraint_relaxed is tiered.roe_constraint_relaxed is False
    assert joint.selected_return_on_equity == pytest.approx(tiered.selected_return_on_equity)
