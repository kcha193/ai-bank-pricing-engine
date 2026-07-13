"""Tests for the application-layer PricingService orchestrator."""

import json

import pytest

from balance_pricing_engine.application.contracts import (
    PricingScenarioRequest,
)
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.events.models import DomainEvent
from balance_pricing_engine.events.publisher import InMemoryEventPublisher
from balance_pricing_engine.governance.gates import DecisionStatus
from balance_pricing_engine.modelling.agentic import ModelRegistry
from balance_pricing_engine.modelling.datasets import LOAN_FEATURE_COLUMNS
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


class _FailingEventPublisher:
    def publish(self, event: DomainEvent) -> None:
        raise RuntimeError("simulated event outage")


def _small_config():
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 500, "n_deposit_customers": 500}
    )
    return config.model_copy(update={"simulation": simulation})


def test_run_scenario_matches_baseline_recommendation_exactly():
    """PricingService must not alter the underlying optimiser recommendation."""
    config = _small_config()
    baseline = run_baseline_diagnostics(config)

    service = PricingService()
    decision = service.run_scenario(PricingScenarioRequest(config=config))

    assert decision.best_loan_rate == baseline.best_result.loan_rate
    assert decision.best_deposit_rate == baseline.best_result.deposit_rate
    assert decision.objective_value == pytest.approx(baseline.best_result.total_objective)


def test_run_scenario_uses_deterministic_response_without_registry():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config))

    assert decision.model_lineage["loan"].response_source == "deterministic_synthetic"
    assert decision.model_lineage["deposit"].response_source == "deterministic_synthetic"
    assert decision.decision_record.used_deterministic_response is True


def test_run_scenario_generates_correlation_id_when_not_supplied():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config))

    assert decision.correlation_id  # non-empty
    assert decision.decision_id


def test_run_scenario_propagates_supplied_correlation_id():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(config=config, correlation_id="my-correlation-id")
    )

    assert decision.correlation_id == "my-correlation-id"
    assert decision.decision_record.correlation_id == "my-correlation-id"


def test_run_scenario_publishes_expected_event_sequence():
    config = _small_config()
    publisher = InMemoryEventPublisher()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(config=config, event_publisher=publisher)
    )

    event_types = [event.event_type for event in publisher.events]
    assert event_types[0] == "PricingScenarioRequested"
    assert "ModelResolved" in event_types
    assert "PricingDecisionProduced" in event_types
    for event in publisher.events:
        assert event.correlation_id == decision.correlation_id


def test_run_scenario_publishes_governance_review_event_when_review_required():
    config = _small_config()
    publisher = InMemoryEventPublisher()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(config=config, event_publisher=publisher)
    )

    event_types = [event.event_type for event in publisher.events]
    if decision.status is DecisionStatus.REVIEW_REQUIRED:
        assert "GovernanceReviewRequired" in event_types
    elif decision.status is DecisionStatus.BLOCKED:
        assert "PricingDecisionBlocked" in event_types


def test_run_scenario_with_failing_event_publisher_still_returns_decision():
    """A broken event publisher must never fail or alter a pricing decision."""
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(config=config, event_publisher=_FailingEventPublisher())
    )

    assert decision.best_loan_rate > 0
    assert decision.status in (
        DecisionStatus.RECOMMENDED,
        DecisionStatus.REVIEW_REQUIRED,
        DecisionStatus.BLOCKED,
    )


def test_persist_ledger_requires_config_path():
    config = _small_config()

    with pytest.raises(ValueError, match="config_path"):
        PricingScenarioRequest(config=config, persist_ledger=True)


def test_run_scenario_persists_ledger_and_additive_decision_record(tmp_path):
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(
            config=config,
            scenario_name="ledger_test",
            config_path="configs/base.yaml",
            persist_ledger=True,
            ledger_base_dir=tmp_path,
        )
    )

    assert decision.run_dir is not None
    assert decision.run_id is not None
    run_files = {p.name for p in decision.run_dir.iterdir()}
    # Existing stable artifacts are untouched.
    assert {"summary.json", "metadata.json", "grid_results.csv", "pricing_memo.md"} <= run_files
    # New additive artifact is present.
    assert "decision_record.json" in run_files

    decision_record_payload = json.loads(
        (decision.run_dir / "decision_record.json").read_text(encoding="utf-8")
    )
    assert decision_record_payload["decision_id"] == decision.decision_id
    assert decision_record_payload["run_id"] == decision.run_id


def test_run_scenario_without_persist_ledger_has_no_run_dir():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config))

    assert decision.run_dir is None
    assert decision.run_id is None


def test_run_scenario_with_broken_approved_champion_raises_governance_error(tmp_path):
    """Fail-loud invariant: PricingService must not downgrade GovernanceError."""
    config = _small_config()
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "BrokenChampion",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(tmp_path / "does_not_exist.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )
    service = PricingService()

    with pytest.raises(GovernanceError):
        service.run_scenario(PricingScenarioRequest(config=config, registry=registry))


def test_run_scenario_with_unapproved_champion_falls_back_to_synthetic(tmp_path):
    config = _small_config()
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "PendingChampion",
            "champion_status": "champion",
            "governance_status": "pending_human_approval",
            "artifact_path": str(tmp_path / "does_not_exist.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )
    baseline = run_baseline_diagnostics(config)
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config, registry=registry))

    assert decision.best_loan_rate == baseline.best_result.loan_rate
    assert decision.model_lineage["loan"].response_source == "deterministic_synthetic"


def test_run_scenario_records_stage_durations():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config))

    stage_durations = decision.decision_record.stage_durations_seconds
    assert "feature_resolution" in stage_durations
    assert "optimisation" in stage_durations
    assert "model_resolution" in stage_durations
    assert "governance_evaluation" in stage_durations
    assert all(duration >= 0 for duration in stage_durations.values())


def test_run_scenario_includes_sensitivity_when_requested():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(
        PricingScenarioRequest(config=config, include_sensitivity=True)
    )

    assert decision.sensitivity is not None
    assert "scenario_name" in decision.sensitivity.columns


def test_run_scenario_feature_lineage_covers_both_products():
    config = _small_config()
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config))

    assert set(decision.feature_lineage) == {"loan", "deposit"}
    for lineage in decision.feature_lineage.values():
        assert lineage.schema_fingerprint
