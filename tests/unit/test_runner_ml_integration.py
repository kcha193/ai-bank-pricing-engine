"""End-to-end coverage for wiring approved ML champions into run_baseline_diagnostics.

The unit tests in ``test_modelling_agentic.py`` already cover
``ModelScoringAgent.resolve_response_fn`` in isolation. These tests cover the
*integration* path added when ``run_baseline_diagnostics`` gained a ``registry``
argument: that an approved champion actually flows through the adaptive grid
search, and — most importantly — that an *un-approved* model can never move the
recommendation (the governance invariant).
"""
import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.agentic import ElasticityBaselineModel, ModelRegistry
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    LOAN_FEATURE_COLUMNS,
)
from balance_pricing_engine.modelling.persistence import save_model
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


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


def test_approved_champion_is_used_end_to_end(tmp_path):
    """An approved champion's response model actually flows into the optimizer."""
    config = _small_config()
    baseline = run_baseline_diagnostics(config)

    loan_artifact = tmp_path / "loan.joblib"
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(ElasticityBaselineModel(), loan_artifact)
    save_model(ElasticityBaselineModel(), deposit_artifact)

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "LoanChampion",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(loan_artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )
    registry.register(
        {
            "model_name": "DepositChampion",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(deposit_artifact),
            "feature_columns": DEPOSIT_FEATURE_COLUMNS,
        }
    )

    result = run_baseline_diagnostics(config, registry=registry)

    # The adapter scored the whole grid without error and produced a valid result.
    # Volumes are dollar-weighted (probability * amount), so they are simply positive.
    assert result.best_result.expected_loan_volume > 0
    assert result.best_result.expected_deposit_volume > 0
    # The champion's probabilities differ from the synthetic fallback, so the
    # objective must move away from the synthetic baseline — proving the ML
    # response function genuinely reached the optimizer.
    assert result.best_result.total_objective != pytest.approx(
        baseline.best_result.total_objective
    )
