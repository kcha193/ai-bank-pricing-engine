"""Application-layer contracts for the governed pricing decision workflow.

These dataclasses are the narrow request/response boundary between delivery
surfaces (API, CLI, examples) and the domain core. They intentionally do not
import FastAPI or any delivery-layer dependency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.events.publisher import EventPublisher, NoOpEventPublisher
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.governance.decision_record import DecisionRecord
from balance_pricing_engine.governance.gates import DecisionStatus, GateResult
from balance_pricing_engine.governance.lineage import ModelLineage
from balance_pricing_engine.governance.shadow import ShadowEvaluationResult


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class ExecutionContext:
    """Correlates one pricing-service invocation across logs/events/records."""

    correlation_id: str = field(default_factory=new_id)
    decision_id: str = field(default_factory=new_id)


@dataclass
class PricingScenarioRequest:
    """Request contract accepted by ``PricingService.run_scenario``.

    ``config`` must already be a validated ``EngineConfig`` — validation is
    the delivery layer's responsibility (YAML parsing, pydantic errors), not
    the application service's.
    """

    config: EngineConfig
    scenario_name: str = "scenario"
    config_path: str | Path | None = None
    correlation_id: str | None = None
    registry: Any | None = None  # Optional[ModelRegistry]; kept as Any to avoid a modelling import
    event_publisher: EventPublisher = field(default_factory=NoOpEventPublisher)
    include_sensitivity: bool = False
    persist_ledger: bool = False
    ledger_base_dir: str | Path = "data/outputs/runs"
    competitor_rate_source: str | None = None
    enable_shadow_challenger: bool = False
    max_boundary_expansions: int = 2

    def __post_init__(self) -> None:
        if self.persist_ledger and self.config_path is None:
            raise ValueError(
                "persist_ledger=True requires config_path (the source YAML file) so the "
                "run ledger can snapshot it verbatim."
            )


@dataclass(frozen=True)
class GovernanceSummary:
    """Aggregated governance evaluation for one pricing decision."""

    status: DecisionStatus
    gates: tuple[GateResult, ...]
    blocking_reasons: tuple[str, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PricingDecision:
    """Stable application-level output of one governed pricing scenario run."""

    decision_id: str
    correlation_id: str
    status: DecisionStatus
    scenario_name: str
    best_loan_rate: float
    best_deposit_rate: float
    objective_value: float
    expected_loan_volume: float
    expected_deposit_volume: float
    funding_shortfall: float
    boundary_warning_flag: bool
    initial_boundary_warning_flag: bool
    expansion_rounds: int
    expansion_capped: bool
    policy_band_breached: bool
    governance: GovernanceSummary
    model_lineage: dict[str, ModelLineage]
    feature_lineage: dict[str, FeatureLineage]
    execution_duration_seconds: float
    decision_record: DecisionRecord
    diagnostics: Any  # ScenarioDiagnostics; full object kept for backward-compatible access
    run_id: str | None = None
    run_dir: Path | None = None
    shadow_evaluations: tuple[ShadowEvaluationResult, ...] = ()
    sensitivity: Any = None  # Optional[pd.DataFrame]
