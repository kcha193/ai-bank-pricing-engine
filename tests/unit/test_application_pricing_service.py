"""Tests for the application-layer PricingService orchestrator."""

import hashlib
import json
import threading
import time
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest
import yaml

from balance_pricing_engine.application import pricing_service
from balance_pricing_engine.application.contracts import (
    PricingScenarioRequest,
)
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.competitor_rates.apply import apply_competitor_rates_to_config
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.events.models import DomainEvent, pricing_scenario_requested
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


def _small_segmented_config():
    config = load_config("configs/segmented_pricing.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 500, "n_deposit_customers": 500}
    )
    return config.model_copy(update={"simulation": simulation})


def _competitor_rate_config(tmp_path, *, validation_status: str = "valid") -> EngineConfig:
    run = load_rate_run("tests/fixtures/competitor_rates/latest.json")
    if validation_status != "valid":
        run = run.model_copy(
            update={
                "validation_summary": run.validation_summary.model_copy(
                    update={"status": validation_status, "error_count": 1}
                )
            }
        )
    source_file = tmp_path / "latest.json"
    source_file.write_text(run.model_dump_json(), encoding="utf-8")
    raw_config = _small_config().model_dump()
    raw_config["competitor_rates"] = {
        **raw_config["competitor_rates"],
        "enabled": True,
        "source_file": str(source_file),
        "max_staleness_hours": 10_000,
        "strict": True,
    }
    return EngineConfig.model_validate(raw_config)


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


def test_run_scenario_bounds_total_publish_wait_to_one_request_wide_deadline(monkeypatch):
    """_run_scenario calls publish_safely several times per request (request + two
    model-resolution + outcome events). Joining a fresh DEFAULT_PUBLISH_DEADLINE_SECONDS
    on every one of those calls against a hung publisher would let a single broken
    publisher add several multiples of that deadline to every request; a shared
    request-wide budget must keep the total added wait near one deadline's worth
    regardless of how many events the request happens to publish.
    """
    monkeypatch.setattr(pricing_service, "DEFAULT_PUBLISH_DEADLINE_SECONDS", 0.2)
    config = _small_config()

    baseline_start = time.monotonic()
    PricingService().run_scenario(
        PricingScenarioRequest(config=config, event_publisher=InMemoryEventPublisher())
    )
    baseline_elapsed = time.monotonic() - baseline_start

    hang = threading.Event()

    class _HangingPublisher:
        def publish(self, event: DomainEvent) -> None:
            hang.wait()

    hung_start = time.monotonic()
    try:
        decision = PricingService().run_scenario(
            PricingScenarioRequest(config=config, event_publisher=_HangingPublisher())
        )
    finally:
        hang.set()
    hung_elapsed = time.monotonic() - hung_start

    assert decision.best_loan_rate > 0
    publish_overhead = hung_elapsed - baseline_elapsed
    # A per-call deadline would add roughly 4x DEFAULT_PUBLISH_DEADLINE_SECONDS (request +
    # 2 model-resolution + outcome events); a shared budget adds roughly one deadline's
    # worth. 3x leaves headroom above the fixed behaviour while still catching a
    # regression back to per-call waiting.
    assert publish_overhead < 3 * 0.2


def test_publish_budget_does_not_charge_for_time_outside_publish_calls():
    """The budget must track cumulative time actually spent inside publish() calls, not
    wall-clock time since it was created -- an earlier version anchored an absolute
    deadline at request start, so slow unrelated work (feature resolution, grid-search
    optimisation) between publishes silently consumed the whole budget before the first
    real publish, forcing every later call to a 0s join even with a healthy publisher.
    """
    budget = pricing_service._RequestPublishBudget(total_seconds=0.2)
    publisher = InMemoryEventPublisher()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    budget.publish(publisher, event)  # fast/healthy call, consumes ~0s of the budget
    time.sleep(0.3)  # simulate slow work between publishes (e.g. optimisation)

    assert budget.remaining > 0.15


def test_publish_budget_decreases_by_actual_publish_wait_time():
    budget = pricing_service._RequestPublishBudget(total_seconds=0.2)
    hang = threading.Event()

    class _HangingPublisher:
        def publish(self, event: DomainEvent) -> None:
            hang.wait()

    event = pricing_scenario_requested("corr-1", scenario_name="baseline")
    try:
        budget.publish(_HangingPublisher(), event)
    finally:
        hang.set()

    assert budget.remaining < 0.05


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


def test_run_scenario_segmented_pricing_ignores_registry_and_records_synthetic_lineage(tmp_path):
    """Guard: segmented runs never call ModelScoringAgent.resolve_response_fn (see
    scenarios/runner.py), so lineage must not claim an approved champion drove the
    response even when the caller supplies a registry with an approved champion."""
    config = _small_segmented_config()
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "SegmentedIrrelevantChampion",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(tmp_path / "does_not_exist.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )
    service = PricingService()

    decision = service.run_scenario(PricingScenarioRequest(config=config, registry=registry))

    assert decision.model_lineage["loan"].response_source == "deterministic_synthetic"
    assert decision.model_lineage["loan"].used_fallback is True
    assert decision.model_lineage["deposit"].response_source == "deterministic_synthetic"
    assert decision.model_lineage["deposit"].used_fallback is True


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


def test_run_scenario_rejects_failed_strict_competitor_rates_before_pricing(tmp_path, monkeypatch):
    config = _competitor_rate_config(tmp_path, validation_status="failed")

    def _optimisation_must_not_run(*args, **kwargs):
        pytest.fail("optimisation must not run for failed strict competitor-rate data")

    monkeypatch.setattr(
        "balance_pricing_engine.application.pricing_service.run_baseline_diagnostics",
        _optimisation_must_not_run,
    )

    with pytest.raises(ValueError, match="failed validation"):
        PricingService().run_scenario(PricingScenarioRequest(config=config))


def test_run_scenario_applies_enabled_competitor_rates_before_optimisation(tmp_path, monkeypatch):
    config = _competitor_rate_config(tmp_path)
    captured: dict[str, EngineConfig] = {}

    def _capture_config(effective_config, **kwargs):
        captured["config"] = effective_config
        return run_baseline_diagnostics(effective_config, **kwargs)

    monkeypatch.setattr(
        "balance_pricing_engine.application.pricing_service.run_baseline_diagnostics",
        _capture_config,
    )

    PricingService().run_scenario(
        PricingScenarioRequest(config=config, max_boundary_expansions=0)
    )

    assert captured["config"].market.competitor_loan_rate == pytest.approx((5.5 + 5.7 + 5.6 + 5.8) / 4 / 100)
    assert captured["config"].market.competitor_deposit_rate == pytest.approx(
        (4.1 + 4.3 + 4.4 + 4.2 + 4.5) / 5 / 100
    )


def test_run_scenario_persists_effective_config_snapshot_when_competitor_rates_applied(
    tmp_path,
):
    """The ledger snapshot/hash must reflect the resolved competitor rates that
    actually drove pricing, not the caller's original (pre-resolution) YAML at
    config_path -- otherwise two different competitor-rate files could produce
    different decisions with an identical archived config."""
    config = _competitor_rate_config(tmp_path)
    original_config_path = tmp_path / "original_config.yaml"
    original_config_path.write_text(
        yaml.safe_dump(load_config("configs/base.yaml").model_dump(mode="json")),
        encoding="utf-8",
    )

    decision = PricingService().run_scenario(
        PricingScenarioRequest(
            config=config,
            scenario_name="competitor_ledger_test",
            config_path=str(original_config_path),
            persist_ledger=True,
            ledger_base_dir=tmp_path / "runs",
            max_boundary_expansions=0,
        )
    )

    metadata = json.loads((decision.run_dir / "metadata.json").read_text(encoding="utf-8"))
    # config_path is recorded verbatim for traceability...
    assert metadata["config_path"] == str(original_config_path)
    # ...but the hash and snapshot must NOT match the untouched original file, since the
    # priced market rates differ from what that file contains.
    original_hash = hashlib.sha256(original_config_path.read_bytes()).hexdigest()
    assert metadata["config_hash"] != original_hash
    assert metadata["config_hash"] == decision.decision_record.config_hash

    snapshot = yaml.safe_load(
        (decision.run_dir / "config_snapshot.yaml").read_text(encoding="utf-8")
    )
    resolved_config = apply_competitor_rates_to_config(config)
    assert snapshot["market"]["competitor_loan_rate"] == pytest.approx(
        resolved_config.market.competitor_loan_rate
    )


def test_run_scenario_cleans_up_materialized_effective_config_temp_file(tmp_path, monkeypatch):
    """The temp YAML written by _materialize_effective_config must not leak: it is
    only ever consumed within run_scenario (config hash + ledger snapshot), so
    PricingService owns its lifecycle and must delete it once the scenario
    completes."""
    config = _competitor_rate_config(tmp_path)
    original_config_path = tmp_path / "original_config.yaml"
    original_config_path.write_text(
        yaml.safe_dump(load_config("configs/base.yaml").model_dump(mode="json")),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    original_materialize = pricing_service._materialize_effective_config

    def _capture_materialize(cfg, config_path):
        result = original_materialize(cfg, config_path)
        captured["path"] = result
        return result

    monkeypatch.setattr(pricing_service, "_materialize_effective_config", _capture_materialize)

    PricingService().run_scenario(
        PricingScenarioRequest(
            config=config,
            config_path=str(original_config_path),
            max_boundary_expansions=0,
        )
    )

    assert captured["path"] is not None
    assert not captured["path"].exists()


def test_run_scenario_cleans_up_materialized_effective_config_temp_file_on_failure(
    tmp_path, monkeypatch
):
    """The materialized temp config must be cleaned up even when run_scenario
    raises partway through -- a failed scenario must not leak the temp file."""
    config = _competitor_rate_config(tmp_path)
    original_config_path = tmp_path / "original_config.yaml"
    original_config_path.write_text(
        yaml.safe_dump(load_config("configs/base.yaml").model_dump(mode="json")),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    original_materialize = pricing_service._materialize_effective_config

    def _capture_materialize(cfg, config_path):
        result = original_materialize(cfg, config_path)
        captured["path"] = result
        return result

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pricing_service, "_materialize_effective_config", _capture_materialize)
    monkeypatch.setattr(
        "balance_pricing_engine.application.pricing_service.run_baseline_diagnostics", _raise
    )

    with pytest.raises(RuntimeError, match="boom"):
        PricingService().run_scenario(
            PricingScenarioRequest(
                config=config,
                config_path=str(original_config_path),
                max_boundary_expansions=0,
            )
        )

    assert captured["path"] is not None
    assert not captured["path"].exists()


def test_evaluate_shadow_returns_available_product_results(monkeypatch):
    config = _small_config()
    diagnostics = SimpleNamespace(
        best_result=SimpleNamespace(loan_rate=0.06, deposit_rate=0.045)
    )
    expected = {
        "loan": SimpleNamespace(product="loan"),
        "deposit": SimpleNamespace(product="deposit"),
    }

    monkeypatch.setattr(
        pricing_service,
        "evaluate_shadow_challenger",
        lambda _registry, _features, product, _frame: expected[product],
    )

    results = PricingService()._evaluate_shadow(
        PricingScenarioRequest(config=config, registry=object()), diagnostics
    )

    assert results == [expected["loan"], expected["deposit"]]


def test_publish_outcome_event_emits_status_specific_and_produced_events():
    config = _small_config()
    diagnostics = SimpleNamespace(
        best_result=SimpleNamespace(loan_rate=0.06, deposit_rate=0.045, total_objective=1.0)
    )
    service = PricingService()

    blocked_publisher = InMemoryEventPublisher()
    service._publish_outcome_event(
        PricingScenarioRequest(config=config, event_publisher=blocked_publisher),
        "correlation-id",
        "decision-id",
        DecisionStatus.BLOCKED,
        ("failed gate",),
        (),
        "run-id",
        diagnostics,
    )
    assert [event.event_type for event in blocked_publisher.events] == [
        "PricingDecisionBlocked",
        "PricingDecisionProduced",
    ]

    review_publisher = InMemoryEventPublisher()
    service._publish_outcome_event(
        PricingScenarioRequest(config=config, event_publisher=review_publisher),
        "correlation-id",
        "decision-id",
        DecisionStatus.REVIEW_REQUIRED,
        (),
        ("review gate",),
        None,
        diagnostics,
    )
    assert [event.event_type for event in review_publisher.events] == [
        "GovernanceReviewRequired",
        "PricingDecisionProduced",
    ]


def test_pricing_service_helpers_handle_unavailable_optional_data(tmp_path, monkeypatch):
    config = _small_config()
    assert pricing_service._reference_or_competitor(None, 0.05) == 0.05
    assert pricing_service._read_run_id(tmp_path) is None
    assert pricing_service._shadow_payload([]) is None

    def _unavailable_version(_package: str) -> str:
        raise PackageNotFoundError

    def _unavailable_sensitivity(_config):
        raise NotImplementedError

    def _unavailable_git(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(pricing_service, "_pkg_version", _unavailable_version)
    monkeypatch.setattr(
        "balance_pricing_engine.diagnostics.run_sensitivity_analysis",
        _unavailable_sensitivity,
    )
    monkeypatch.setattr(pricing_service.subprocess, "run", _unavailable_git)

    assert pricing_service._package_version() is None
    assert pricing_service._run_sensitivity_safely(config) is None
    assert pricing_service._git_commit() is None


def test_competitor_rate_issues_report_missing_and_non_strict_failed_sources(tmp_path):
    missing_source_config = _competitor_rate_config(tmp_path)
    missing_source_config = missing_source_config.model_copy(
        update={
            "competitor_rates": missing_source_config.competitor_rates.model_copy(
                update={"source_file": str(tmp_path / "missing.json")}
            )
        }
    )
    missing_issues = pricing_service._competitor_rate_issues(missing_source_config)

    assert missing_issues is not None
    assert missing_issues[0].severity == "error"

    non_strict_failed_config = _competitor_rate_config(tmp_path, validation_status="failed")
    non_strict_failed_config = non_strict_failed_config.model_copy(
        update={
            "competitor_rates": non_strict_failed_config.competitor_rates.model_copy(
                update={"strict": False}
            )
        }
    )
    failed_issues = pricing_service._competitor_rate_issues(non_strict_failed_config)

    assert failed_issues is not None
    assert failed_issues[0].severity == "warning"
    assert failed_issues[0].message == "Competitor-rate source file failed validation."
