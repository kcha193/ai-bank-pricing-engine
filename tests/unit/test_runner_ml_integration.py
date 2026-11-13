"""End-to-end coverage for wiring approved ML champions into run_baseline_diagnostics.

The unit tests in ``test_modelling_agentic.py`` already cover
``ModelScoringAgent.resolve_response_fn`` in isolation. These tests cover the
*integration* path added when ``run_baseline_diagnostics`` gained a ``registry``
argument: that an approved champion actually flows through the adaptive grid
search, and — most importantly — that an *un-approved* model can never move the
recommendation (the governance invariant).
"""
import hashlib

import numpy as np
import pytest

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.agentic import ElasticityBaselineModel, ModelRegistry
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.models import build_logistic_regression_model
from balance_pricing_engine.modelling.persistence import save_model
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng


def _small_config():
    """Baseline config with a reduced cohort so multiple runs stay fast."""
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 1000, "n_deposit_customers": 1000}
    )
    return config.model_copy(update={"simulation": simulation})


def test_unapproved_champion_does_not_change_recommendation(tmp_path):
    """Governance invariant: a champion that is not approved must be ignored."""
    config = _small_config()
    baseline = run_baseline_diagnostics(config)

    artifact = tmp_path / "loan_model.joblib"
    save_model(ElasticityBaselineModel(), artifact)

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "LogisticResponseModel",
            "champion_status": "champion",
            "governance_status": "pending",  # NOT approved
            "artifact_path": str(artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    governed = run_baseline_diagnostics(config, registry=registry)

    assert governed.best_result.loan_rate == baseline.best_result.loan_rate
    assert governed.best_result.deposit_rate == baseline.best_result.deposit_rate
    assert governed.best_result.total_objective == pytest.approx(
        baseline.best_result.total_objective
    )


def test_approved_champion_with_missing_artifact_raises(tmp_path):
    """An approved champion whose artifact is missing must fail loud, not silently
    reprice on the synthetic fallback. A run that the registry claims used an
    approved champion but actually used synthetic would be an audit-integrity hole."""
    config = _small_config()

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "LogisticResponseModel",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(tmp_path / "does_not_exist.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    with pytest.raises(GovernanceError, match="missing"):
        run_baseline_diagnostics(config, registry=registry)


def _trained_champion_registry(tmp_path):
    """Registry whose loan and deposit champions went through verified approval."""
    rng = make_rng(11)
    loan_ds = build_loan_response_dataset(
        generate_loan_customers(300, rng),
        offered_rates=np.array([0.045, 0.055, 0.065, 0.075]),
        competitor_rate=0.0499,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=2000,
    )
    deposit_ds = build_deposit_response_dataset(
        generate_deposit_customers(300, rng),
        offered_rates=np.array([0.03, 0.04, 0.05, 0.06]),
        competitor_rate=0.039,
        rate_elasticity=80.0,
        competitor_gap_elasticity=60.0,
        rng=rng,
        n_samples=2000,
    )
    loan_artifact = tmp_path / "loan.joblib"
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(
        build_logistic_regression_model(42).fit(
            loan_ds[LOAN_FEATURE_COLUMNS], loan_ds[LOAN_TARGET_COLUMN]
        ),
        loan_artifact,
    )
    save_model(
        build_logistic_regression_model(42).fit(
            deposit_ds[DEPOSIT_FEATURE_COLUMNS], deposit_ds[DEPOSIT_TARGET_COLUMN]
        ),
        deposit_artifact,
    )

    registry = ModelRegistry(tmp_path / "registry.json")
    for name, artifact, features in (
        ("LoanChampion", loan_artifact, LOAN_FEATURE_COLUMNS),
        ("DepositChampion", deposit_artifact, DEPOSIT_FEATURE_COLUMNS),
    ):
        registry.register(
            {
                "model_name": name,
                "champion_status": "challenger",
                "governance_status": "pending_human_approval",
                "artifact_path": str(artifact),
                "feature_columns": features,
            }
        )
    registry.promote_champion("LoanChampion", approver="validator", product="loan")
    registry.promote_champion("DepositChampion", approver="validator", product="deposit")
    return registry, loan_artifact


def test_approved_champion_is_used_end_to_end(tmp_path):
    """A verified approved champion's response model actually flows into the optimizer."""
    config = _small_config()
    baseline = run_baseline_diagnostics(config)
    registry, _ = _trained_champion_registry(tmp_path)

    result = run_baseline_diagnostics(config, registry=registry)

    # Volumes are dollar-weighted (probability * amount), so they are simply positive.
    assert result.best_result.expected_loan_volume > 0
    assert result.best_result.expected_deposit_volume > 0
    # The champion's probabilities differ from the synthetic fallback, so the
    # objective must move away from the synthetic baseline — proving the ML
    # response function genuinely reached the optimizer.
    assert result.best_result.total_objective != pytest.approx(
        baseline.best_result.total_objective
    )


def test_t13_pricing_service_propagates_failed_champion_validation(tmp_path):
    """An approved champion whose artifact changed after approval must raise
    GovernanceError out of PricingService.run_scenario, not become a decision."""
    config = _small_config()
    registry, loan_artifact = _trained_champion_registry(tmp_path)
    loan_artifact.write_bytes(loan_artifact.read_bytes() + b"tampered")

    with pytest.raises(GovernanceError, match="SHA-256 mismatch"):
        PricingService().run_scenario(PricingScenarioRequest(config=config, registry=registry))


def test_t13_pricing_service_records_integrity_lineage_for_verified_champion(tmp_path):
    config = _small_config()
    registry, loan_artifact = _trained_champion_registry(tmp_path)

    decision = PricingService().run_scenario(
        PricingScenarioRequest(config=config, registry=registry)
    )

    loan = decision.model_lineage["loan"]
    assert loan.response_source == "approved_ml_champion"
    assert loan.artifact_sha256 == hashlib.sha256(loan_artifact.read_bytes()).hexdigest()
    assert loan.feature_names == tuple(LOAN_FEATURE_COLUMNS)
    assert loan.integrity_check == "passed"
    assert loan.prediction_check == "passed"
    gate = next(
        g for g in decision.governance.gates if g.code == "MODEL_ARTIFACT_AVAILABLE[loan]"
    )
    assert gate.status.value == "PASS"
    assert gate.evidence["artifact_sha256"] == loan.artifact_sha256


def test_t14_synthetic_baseline_pricing_is_unchanged():
    """Pinned pre-Phase-1b synthetic baseline for the reduced cohort (no registry)."""
    result = run_baseline_diagnostics(_small_config()).best_result

    assert result.loan_rate == pytest.approx(0.0619)
    assert result.deposit_rate == pytest.approx(0.041)
    assert result.total_objective == pytest.approx(1178733.185003899, rel=1e-12)
