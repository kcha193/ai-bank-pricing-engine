"""Application pricing service: orchestrates the domain core end to end.

This is the seam described in CLAUDE.md's target architecture: API/CLI/example
callers depend on ``PricingService`` instead of calling
``run_baseline_diagnostics`` directly, so governance evaluation, lineage
recording, event publication, and ledger persistence happen consistently in
one place. The domain math (``run_baseline_diagnostics``,
``run_sensitivity_analysis``, ``write_run_ledger``) is unchanged and still
does all the pricing work — this module only orchestrates around it.

Governance invariant (preserved, not re-implemented here): ``GovernanceError``
raised by ``ModelScoringAgent.resolve_response_fn`` (via
``run_baseline_diagnostics(registry=...)``) propagates out of
``run_scenario`` unchanged. It is never downgraded to a ``BLOCKED``
``PricingDecision`` — an inconsistent governed model state must fail loud, not
be represented as a normal (if blocked) decision. Delivery layers translate
the exception into a transport-appropriate error response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from balance_pricing_engine.application.contracts import (
    GovernanceSummary,
    PricingDecision,
    PricingScenarioRequest,
)
from balance_pricing_engine.competitor_rates.apply import apply_competitor_rates_to_config
from balance_pricing_engine.competitor_rates.models import ValidationIssue
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_staleness
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.events.models import (
    governance_review_required,
    model_resolved,
    pricing_decision_blocked,
    pricing_decision_produced,
    pricing_scenario_requested,
    run_ledger_written,
)
from balance_pricing_engine.events.publisher import publish_safely
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.features.local_store import LocalFeatureStore
from balance_pricing_engine.governance.decision_record import (
    build_decision_record,
    write_decision_record,
)
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.governance.lineage import ModelLineage, resolve_model_lineage
from balance_pricing_engine.governance.shadow import (
    ShadowEvaluationResult,
    evaluate_shadow_challenger,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    LOAN_FEATURE_COLUMNS,
)
from balance_pricing_engine.observability.logging import ExecutionMetrics, log_event, stage_timer
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.run_ledger import write_run_ledger
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng

logger = logging.getLogger(__name__)


class PricingService:
    """Narrow orchestrator for one governed pricing-scenario request."""

    def __init__(self, feature_store: LocalFeatureStore | None = None):
        self._feature_store = feature_store or LocalFeatureStore()

    def run_scenario(self, request: PricingScenarioRequest) -> PricingDecision:
        request = replace(request, config=apply_competitor_rates_to_config(request.config))
        correlation_id = request.correlation_id or str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        metrics = ExecutionMetrics(correlation_id=correlation_id, decision_id=decision_id)
        start = time.monotonic()

        log_event(
            logger,
            "pricing_scenario.started",
            correlation_id=correlation_id,
            decision_id=decision_id,
            scenario_name=request.scenario_name,
        )
        publish_safely(
            request.event_publisher,
            pricing_scenario_requested(correlation_id, scenario_name=request.scenario_name),
        )

        with stage_timer(metrics, "feature_resolution"):
            feature_lineage = self._resolve_feature_lineage(request.config)

        try:
            with stage_timer(metrics, "optimisation"):
                diagnostics = run_baseline_diagnostics(
                    request.config,
                    max_boundary_expansions=request.max_boundary_expansions,
                    registry=request.registry,
                )
        except GovernanceError:
            log_event(
                logger,
                "pricing_scenario.governance_error",
                level=logging.ERROR,
                correlation_id=correlation_id,
                decision_id=decision_id,
                scenario_name=request.scenario_name,
            )
            raise

        with stage_timer(metrics, "model_resolution"):
            model_lineage = self._resolve_model_lineage(request, correlation_id, decision_id)

        shadow_evaluations: list[ShadowEvaluationResult] = []
        if request.enable_shadow_challenger and request.registry is not None:
            with stage_timer(metrics, "shadow_evaluation"):
                shadow_evaluations = self._evaluate_shadow(request, diagnostics)

        with stage_timer(metrics, "governance_evaluation"):
            competitor_issues = _competitor_rate_issues(request.config)
            gates = evaluate_gates(
                config=request.config,
                diagnostics=diagnostics,
                model_lineage=model_lineage,
                feature_lineage=feature_lineage,
                competitor_rate_issues=competitor_issues,
            )
            status = decision_status_from_gates(gates)
            blocking = tuple(gate.message for gate in gates if gate.status is GateStatus.BLOCK)
            review = tuple(gate.message for gate in gates if gate.status is GateStatus.WARN)
            governance = GovernanceSummary(
                status=status, gates=tuple(gates), blocking_reasons=blocking, review_reasons=review
            )

        sensitivity = None
        if request.include_sensitivity:
            with stage_timer(metrics, "sensitivity_analysis"):
                sensitivity = _run_sensitivity_safely(request.config)

        run_dir: Path | None = None
        run_id: str | None = None
        if request.persist_ledger:
            with stage_timer(metrics, "ledger_writing"):
                run_dir = write_run_ledger(
                    scenario_name=request.scenario_name,
                    config=request.config,
                    config_path=request.config_path,
                    diagnostics=diagnostics,
                    sensitivity=sensitivity,
                    competitor_rate_source=request.competitor_rate_source,
                    base_dir=request.ledger_base_dir,
                )
                run_id = _read_run_id(run_dir)
                publish_safely(
                    request.event_publisher,
                    run_ledger_written(
                        correlation_id,
                        decision_id=decision_id,
                        run_id=run_id or "",
                        run_dir=str(run_dir),
                    ),
                )

        total_duration = time.monotonic() - start
        metrics.total_duration_seconds = total_duration
        metrics.status = status.value

        decision_record = build_decision_record(
            decision_id=decision_id,
            correlation_id=correlation_id,
            scenario_name=request.scenario_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            decision_status=status.value,
            config_hash=_config_hash(request.config, request.config_path),
            package_version=_package_version(),
            git_commit=_git_commit(),
            diagnostics=diagnostics,
            model_lineage=model_lineage,
            feature_lineage=feature_lineage,
            governance_gates=list(gates),
            execution_duration_seconds=total_duration,
            stage_durations_seconds=dict(metrics.stage_durations_seconds),
            run_id=run_id,
            shadow_evaluation=_shadow_payload(shadow_evaluations),
        )
        if run_dir is not None:
            write_decision_record(run_dir, decision_record)

        self._publish_outcome_event(
            request, correlation_id, decision_id, status, blocking, review, run_id, diagnostics
        )
        log_event(
            logger,
            "pricing_scenario.completed",
            correlation_id=correlation_id,
            decision_id=decision_id,
            status=status.value,
            duration_seconds=total_duration,
        )

        return PricingDecision(
            decision_id=decision_id,
            correlation_id=correlation_id,
            status=status,
            scenario_name=request.scenario_name,
            best_loan_rate=float(diagnostics.best_result.loan_rate),
            best_deposit_rate=float(diagnostics.best_result.deposit_rate),
            objective_value=float(diagnostics.best_result.total_objective),
            expected_loan_volume=float(diagnostics.best_result.expected_loan_volume),
            expected_deposit_volume=float(diagnostics.best_result.expected_deposit_volume),
            funding_shortfall=float(diagnostics.best_result.funding_shortfall),
            boundary_warning_flag=bool(diagnostics.boundary_diagnostics.has_warning),
            initial_boundary_warning_flag=bool(diagnostics.initial_boundary_diagnostics.has_warning),
            expansion_rounds=int(diagnostics.expansion_rounds),
            expansion_capped=bool(diagnostics.expansion_capped),
            policy_band_breached=bool(diagnostics.policy_band_breached),
            governance=governance,
            model_lineage=model_lineage,
            feature_lineage=feature_lineage,
            execution_duration_seconds=total_duration,
            decision_record=decision_record,
            diagnostics=diagnostics,
            run_id=run_id,
            run_dir=run_dir,
            shadow_evaluations=tuple(shadow_evaluations),
            sensitivity=sensitivity,
        )

    def _resolve_feature_lineage(self, config: EngineConfig) -> dict[str, FeatureLineage]:
        rng = make_rng(config.random_seed)
        loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
        deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
        _, loan_lineage = self._feature_store.serving_features(
            "loan",
            loan_customers,
            offered_rate=_reference_or_competitor(
                config.market.home_loan_reference_rate, config.market.competitor_loan_rate
            ),
            competitor_rate=config.market.competitor_loan_rate,
        )
        _, deposit_lineage = self._feature_store.serving_features(
            "deposit",
            deposit_customers,
            offered_rate=_reference_or_competitor(
                config.market.term_deposit_reference_rate, config.market.competitor_deposit_rate
            ),
            competitor_rate=config.market.competitor_deposit_rate,
        )
        return {"loan": loan_lineage, "deposit": deposit_lineage}

    def _resolve_model_lineage(
        self,
        request: PricingScenarioRequest,
        correlation_id: str,
        decision_id: str,
    ) -> dict[str, ModelLineage]:
        model_lineage = {
            "loan": resolve_model_lineage(request.registry, LOAN_FEATURE_COLUMNS, "loan"),
            "deposit": resolve_model_lineage(request.registry, DEPOSIT_FEATURE_COLUMNS, "deposit"),
        }
        for product, lineage in model_lineage.items():
            publish_safely(
                request.event_publisher,
                model_resolved(
                    correlation_id,
                    decision_id=decision_id,
                    product=product,
                    response_source=lineage.response_source,
                    model_name=lineage.model_name,
                    used_fallback=lineage.used_fallback,
                ),
            )
        return model_lineage

    def _evaluate_shadow(
        self,
        request: PricingScenarioRequest,
        diagnostics: Any,
    ) -> list[ShadowEvaluationResult]:
        rng = make_rng(request.config.random_seed)
        loan_customers = generate_loan_customers(request.config.simulation.n_loan_customers, rng)
        deposit_customers = generate_deposit_customers(
            request.config.simulation.n_deposit_customers, rng
        )
        loan_features, _ = self._feature_store.serving_features(
            "loan",
            loan_customers,
            offered_rate=diagnostics.best_result.loan_rate,
            competitor_rate=request.config.market.competitor_loan_rate,
        )
        deposit_features, _ = self._feature_store.serving_features(
            "deposit",
            deposit_customers,
            offered_rate=diagnostics.best_result.deposit_rate,
            competitor_rate=request.config.market.competitor_deposit_rate,
        )
        results: list[ShadowEvaluationResult] = []
        loan_result = evaluate_shadow_challenger(
            request.registry, LOAN_FEATURE_COLUMNS, "loan", loan_features
        )
        if loan_result is not None:
            results.append(loan_result)
        deposit_result = evaluate_shadow_challenger(
            request.registry, DEPOSIT_FEATURE_COLUMNS, "deposit", deposit_features
        )
        if deposit_result is not None:
            results.append(deposit_result)
        return results

    def _publish_outcome_event(
        self,
        request: PricingScenarioRequest,
        correlation_id: str,
        decision_id: str,
        status: DecisionStatus,
        blocking: tuple[str, ...],
        review: tuple[str, ...],
        run_id: str | None,
        diagnostics: Any,
    ) -> None:
        if status is DecisionStatus.BLOCKED:
            publish_safely(
                request.event_publisher,
                pricing_decision_blocked(
                    correlation_id,
                    decision_id=decision_id,
                    scenario_name=request.scenario_name,
                    blocking_reasons=list(blocking),
                    run_id=run_id,
                ),
            )
        elif status is DecisionStatus.REVIEW_REQUIRED:
            publish_safely(
                request.event_publisher,
                governance_review_required(
                    correlation_id,
                    decision_id=decision_id,
                    scenario_name=request.scenario_name,
                    review_reasons=list(review),
                    run_id=run_id,
                ),
            )
        publish_safely(
            request.event_publisher,
            pricing_decision_produced(
                correlation_id,
                decision_id=decision_id,
                scenario_name=request.scenario_name,
                decision_status=status.value,
                best_loan_rate=float(diagnostics.best_result.loan_rate),
                best_deposit_rate=float(diagnostics.best_result.deposit_rate),
                objective_value=float(diagnostics.best_result.total_objective),
                run_id=run_id,
            ),
        )


def _reference_or_competitor(reference_rate: float | None, competitor_rate: float) -> float:
    return reference_rate if reference_rate is not None else competitor_rate


def _run_sensitivity_safely(config: EngineConfig) -> Any:
    from balance_pricing_engine.diagnostics import run_sensitivity_analysis

    try:
        return run_sensitivity_analysis(config)
    except NotImplementedError:
        return None


def _competitor_rate_issues(config: EngineConfig) -> list[ValidationIssue] | None:
    competitor_rates = config.competitor_rates
    if competitor_rates is None or not competitor_rates.enabled:
        return None
    source_path = Path(competitor_rates.source_file)
    if not source_path.exists():
        severity = "error" if competitor_rates.strict else "warning"
        return [
            ValidationIssue(
                severity=severity,
                message=f"Competitor rate source file not found: {source_path}",
            )
        ]
    run = load_rate_run(source_path)
    severity = "error" if competitor_rates.strict else "warning"
    validation_issues = [
        issue.model_copy(update={"severity": severity}) for issue in run.validation_summary.issues
    ]
    if run.validation_summary.status == "failed" and not validation_issues:
        validation_issues.append(
            ValidationIssue(
                severity=severity,
                message="Competitor-rate source file failed validation.",
            )
        )
    return validation_issues + validate_staleness(
        run, competitor_rates.max_staleness_hours, competitor_rates.strict
    )


def _read_run_id(run_dir: Path) -> str | None:
    metadata_path = Path(run_dir) / "metadata.json"
    if not metadata_path.exists():
        return None
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    return payload.get("run_id")


def _shadow_payload(
    evaluations: list[ShadowEvaluationResult],
) -> dict[str, Any] | None:
    if not evaluations:
        return None
    return {result.product: result.to_dict() for result in evaluations}


def _config_hash(config: EngineConfig, config_path: str | Path | None) -> str:
    """Config fingerprint: hashes the source YAML file when available, else the
    validated config's JSON representation."""
    if config_path is not None:
        return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _package_version() -> str | None:
    try:
        return _pkg_version("ai-bank-pricing-engine")
    except PackageNotFoundError:
        return None


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None
