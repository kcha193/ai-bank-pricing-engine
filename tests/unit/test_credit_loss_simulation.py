"""Integration checks for FIN-001 annual loss, governance and cohort lineage."""

from dataclasses import replace
import json
from pathlib import Path
import socket

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.config import (
    EngineConfig,
    GridConfig,
    MultiPeriodConfig,
    ProductConfig,
    SegmentedPricingConfig,
    load_config,
)
from balance_pricing_engine.config_identity import effective_config_hash
from balance_pricing_engine.governance.gates import DecisionStatus, evaluate_gates
from balance_pricing_engine.pricing.credit_loss import illustrative_profile
from balance_pricing_engine.pricing.profit import expected_credit_loss
from balance_pricing_engine.scenarios.credit_loss_simulation import (
    CreditLossSimulationConfig,
    run_credit_loss_simulation,
)
from balance_pricing_engine.scenarios.multiperiod import (
    CreditLossMultiPeriodSimulationConfig,
    run_multiperiod_credit_loss_simulation,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.response import loan_acceptance_probability

BASE_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "base.yaml"


@pytest.fixture
def config():
    base = load_config(BASE_CONFIG)
    return base.model_copy(
        update={
            "pricing_strategy": None,
            "products": ProductConfig(
                loan_rate_grid=GridConfig(start=0.052, stop=0.056, step=0.002),
                deposit_rate_grid=GridConfig(start=0.038, stop=0.042, step=0.002),
            ),
        }
    )


@pytest.fixture
def request_config(config):
    return CreditLossSimulationConfig(
        engine_config=config, profile=illustrative_profile(), max_boundary_expansions=0
    )


def test_loss_adapter_applies_acceptance_to_annual_nzd_even_at_zero_principal():
    customers = pd.DataFrame(
        {
            "loan_amount": [0.0, 100.0, 10.0],
            "expected_loss_rate": [0.0, 0.25, 0.25],
            "annual_expected_loss_nzd": [2.0, 8.0, 19.0],
        }
    )
    assert expected_credit_loss(customers, np.array([0.5, 0.25, 0.0])) == 3.0
    assert expected_credit_loss(customers, np.array([1.0, 0.5, 0.0])) == 6.0


@pytest.mark.parametrize(
    ("losses", "probabilities"),
    [
        ([-1.0, 0.0], [0.0, 1.0]),
        ([np.nan, 0.0], [0.0, 1.0]),
        ([np.inf, 0.0], [0.0, 1.0]),
        ([2 + 1j, 0.0], [0.0, 1.0]),
        ([1.0, 2.0], [1.0]),
        ([1.0, 2.0], [1.01, 0.0]),
        ([1.0, 2.0], [0.0, -0.01]),
        ([1.0, 2.0], [np.nan, 0.0]),
        ([np.finfo(float).max] * 2, [1.0, 1.0]),
    ],
)
def test_loss_adapter_rejects_invalid_absolute_inputs(losses, probabilities):
    customers = pd.DataFrame({"annual_expected_loss_nzd": losses})
    with pytest.raises(ValueError, match="loss|probabilities"):
        expected_credit_loss(customers, np.asarray(probabilities))


def test_joint_offer_matches_independent_annual_loss_and_principal(config, request_config):
    simulation = run_credit_loss_simulation(request_config)
    diagnostics = simulation.diagnostics
    best = diagnostics.best_result
    customers = diagnostics.loan_customers
    estimate = simulation.loss_estimate
    probabilities = loan_acceptance_probability(
        customers,
        best.loan_rate,
        config.market.competitor_loan_rate,
        config.response.loan_rate_elasticity,
        config.response.loan_competitor_gap_elasticity,
        coefficients=config.response.coefficients.loan,
    )
    expected_loss = sum(
        probability * sum(
            s.weight * s.pd[i] * s.lgd[i] * s.ead_nzd[i] for s in estimate.scenarios
        )
        for i, probability in enumerate(probabilities)
    )
    expected_principal = float(probabilities @ estimate.principal_nzd)
    assert best.expected_credit_loss == pytest.approx(expected_loss)
    assert best.expected_loan_volume == pytest.approx(expected_principal)
    assert best.loan_interest_income == pytest.approx(expected_principal * best.loan_rate)
    assert best.funding_shortfall == pytest.approx(
        max(config.balance_sheet.required_deposit_funding_ratio * expected_principal
            - best.expected_deposit_volume, 0)
    )
    assert best.total_objective == pytest.approx(
        diagnostics.winner_decomposition().reconciled_total_objective
    )


def test_segmented_one_tier_matches_joint(config, request_config):
    segmented = config.model_copy(
        update={
            "products": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[{"name": "all", "rate_grid": config.products.loan_rate_grid}],
                deposit_tiers=[{"name": "all", "rate_grid": config.products.deposit_rate_grid}],
            ),
        }
    )
    joint = run_credit_loss_simulation(request_config).diagnostics
    tiered = run_credit_loss_simulation(
        request_config.model_copy(update={"engine_config": segmented})
    ).diagnostics
    assert tiered.best_result.loan_rate == joint.best_result.loan_rate
    assert tiered.best_result.deposit_rate == joint.best_result.deposit_rate
    # The segmented summary carries spreads per tier rather than on the aggregate result.
    for name, value in vars(joint.best_result).items():
        if name not in {"home_loan_spread_to_market_bps", "term_deposit_spread_to_market_bps"}:
            assert getattr(tiered.best_result, name) == pytest.approx(value), name


def test_segmented_two_loan_tiers_price_their_own_loss(config, request_config):
    segmented = config.model_copy(
        update={
            "products": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[
                    {"name": "low", "lvr_max": 0.8, "rate_grid": config.products.loan_rate_grid},
                    {"name": "high", "rate_grid": config.products.loan_rate_grid},
                ],
                deposit_tiers=[{"name": "all", "rate_grid": config.products.deposit_rate_grid}],
            ),
        }
    )
    simulation = run_credit_loss_simulation(
        request_config.model_copy(update={"engine_config": segmented})
    )
    diagnostics = simulation.diagnostics
    customers = run_baseline_diagnostics(config, max_boundary_expansions=0).loan_customers
    expected_loss = 0.0
    for name, mask in {"low": customers.lvr <= 0.8, "high": customers.lvr > 0.8}.items():
        probabilities = loan_acceptance_probability(
            customers[mask],
            diagnostics.segmented_tier_rates["loan_tiers"][name],
            config.market.competitor_loan_rate,
            config.response.loan_rate_elasticity,
            config.response.loan_competitor_gap_elasticity,
            coefficients=config.response.coefficients.loan,
        )
        expected_loss += float(probabilities @ simulation.loss_estimate.annual_expected_loss_nzd[mask])
    assert diagnostics.best_result.expected_credit_loss == pytest.approx(expected_loss)


def test_simulation_is_offline_and_cannot_change_governed_baseline(
    config, request_config, monkeypatch, tmp_path
):
    def fail_connect(*args, **kwargs):
        raise AssertionError("Simulation attempted network access")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    service = PricingService()
    governed_request = PricingScenarioRequest(config=config, max_boundary_expansions=0)
    first = service.run_scenario(governed_request)
    simulation = run_credit_loss_simulation(request_config)
    second = service.run_scenario(governed_request)

    assert first.diagnostics.best_result == second.diagnostics.best_result
    assert first.decision_record.config_hash == second.decision_record.config_hash
    assert simulation.governance_status is DecisionStatus.REVIEW_REQUIRED
    assert simulation.diagnostics.best_result.expected_credit_loss != (
        first.diagnostics.best_result.expected_credit_loss
    )
    assert "CREDIT_LOSS_SIMULATION_ONLY" not in {gate.code for gate in first.governance.gates}
    simulation.diagnostics.write_artifacts(tmp_path)

    with pytest.raises(ValidationError):
        EngineConfig.model_validate(request_config.model_dump())
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["credit_loss_simulation"]["profile"]["version"] == request_config.profile.version


def test_simulation_gate_survives_independent_gate_evaluation(config, request_config):
    simulation = run_credit_loss_simulation(request_config)
    gates = evaluate_gates(
        config=config,
        diagnostics=simulation.diagnostics,
        model_lineage={},
        feature_lineage={},
    )
    gate = next(gate for gate in gates if gate.code == "CREDIT_LOSS_SIMULATION_ONLY")
    assert gate.status.value == "WARN"
    assert gate.evidence["profile_version"] == request_config.profile.version
    invalid = replace(
        simulation.diagnostics,
        best_result=replace(simulation.diagnostics.best_result, total_objective=np.nan),
    )
    assert any(
        gate.status.value == "BLOCK"
        for gate in evaluate_gates(
            config=config, diagnostics=invalid, model_lineage={}, feature_lineage={}
        )
    )


def test_simulation_hash_captures_model_and_roundtrips(config, request_config):
    first = run_credit_loss_simulation(request_config)
    recovered = CreditLossSimulationConfig.model_validate_json(request_config.model_dump_json())
    second = run_credit_loss_simulation(recovered)
    other_profile = request_config.profile.model_copy(update={"version": "test-alternate-v2"})
    changed = run_credit_loss_simulation(request_config.model_copy(update={"profile": other_profile}))
    assert first.config_hash == effective_config_hash(first.effective_config)
    assert first.config_hash == second.config_hash
    assert first.config_hash != effective_config_hash(config)
    assert first.config_hash != changed.config_hash
    assert first.diagnostics.best_result == changed.diagnostics.best_result


def test_simulation_rejects_enabled_competitor_ingestion(config, request_config):
    enabled = config.model_copy(update={"competitor_rates": {"enabled": True}})
    with pytest.raises(ValidationError, match="static"):
        request_config.model_copy(update={"engine_config": enabled})


def test_multiperiod_preserves_booked_profile_and_survival(config, request_config):
    config = config.model_copy(
        update={"multiperiod": MultiPeriodConfig(n_periods=2, loan_runoff_rate=0.3)}
    )
    first_profile = request_config.profile
    zero_pd_profile = first_profile.model_copy(
        update={
            "version": "zero-pd-test-v2",
            "scenarios": tuple(
                scenario.model_copy(
                    update={
                        "assumptions": tuple(
                            row.model_copy(update={"pd": 0.0}) for row in scenario.assumptions
                        )
                    }
                )
                for scenario in first_profile.scenarios
            ),
        }
    )
    request = CreditLossMultiPeriodSimulationConfig(
        engine_config=config,
        profiles=(first_profile, zero_pd_profile),
        max_boundary_expansions=0,
    )
    result = run_multiperiod_credit_loss_simulation(request)
    booked_first, booked_second = result.booked_cohorts
    assert booked_first.booked_credit_loss == result.period_diagnostics[0].best_result.expected_credit_loss
    assert booked_first.booked_credit_loss > 0
    assert booked_second.booked_credit_loss == 0
    assert booked_first.credit_loss_profile == first_profile
    assert booked_second.credit_loss_profile == zero_pd_profile
    assert result.period_economics[1].expected_credit_loss == pytest.approx(
        0.7 * booked_first.booked_credit_loss
    )
    assert result.period_economics[1].surviving_loan_volume == pytest.approx(
        0.7 * booked_first.original_loan_volume
    )
    assert result.cohort_summary()["credit_loss_profile_version"].tolist() == [
        first_profile.version, first_profile.version, zero_pd_profile.version
    ]
    assert result.provenance["config_hash"] == effective_config_hash(request)
    assert result.provenance["governance_status"] == "REVIEW_REQUIRED"


def test_multiperiod_rejects_incomplete_or_ambiguous_profile_schedule(config, request_config):
    config = config.model_copy(update={"multiperiod": MultiPeriodConfig(n_periods=2)})
    profile = request_config.profile
    with pytest.raises(ValidationError, match="one profile per period"):
        CreditLossMultiPeriodSimulationConfig(engine_config=config, profiles=(profile,))
    with pytest.raises(ValidationError, match="version"):
        CreditLossMultiPeriodSimulationConfig(
            engine_config=config,
            profiles=(profile, profile.model_copy(update={"review_note": "Changed assumptions."})),
        )


def test_simulation_roundtrip_preserves_default_min_banks(config, request_config):
    one_bank = config.model_copy(update={"pricing_intelligence": {"selected_banks": ["ANZ"]}})
    request = CreditLossSimulationConfig(engine_config=one_bank, profile=request_config.profile)
    clone = request.model_copy()
    decoded = CreditLossSimulationConfig.model_validate_json(request.model_dump_json())
    assert decoded.engine_config.pricing_intelligence.min_banks == 3
    assert effective_config_hash(request) == effective_config_hash(clone)
    assert effective_config_hash(request) == effective_config_hash(decoded)


def test_simulation_snapshot_stays_bound_to_its_hash(request_config):
    result = run_credit_loss_simulation(request_config)
    exposed = result.effective_config
    original_cash_rate = exposed.engine_config.market.cash_rate
    exposed.engine_config.market.cash_rate = original_cash_rate + 0.01
    assert result.effective_config.engine_config.market.cash_rate == original_cash_rate
    assert result.config_hash == effective_config_hash(result.effective_config)
