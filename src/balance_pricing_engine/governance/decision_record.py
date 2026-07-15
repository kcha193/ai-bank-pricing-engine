"""Append-only decision record: the governance-centred audit artifact.

``DecisionRecord`` is written as an additive ``decision_record.json`` artifact
alongside the existing run-ledger files (see
docs/adr/0003-governance-centred-decision-record.md). It never replaces or
renames any existing stable artifact (``summary.json``, ``metadata.json``,
etc.) — those keep their documented schema unchanged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from balance_pricing_engine.features.contracts import FeatureLineage
    from balance_pricing_engine.governance.gates import GateResult
    from balance_pricing_engine.governance.lineage import ModelLineage


@dataclass(frozen=True)
class DecisionRecord:
    """Serialisable, append-only record of one pricing decision."""

    decision_id: str
    correlation_id: str
    scenario_name: str
    timestamp: str
    decision_status: str
    config_hash: str
    package_version: str | None
    git_commit: str | None
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
    model_lineage: dict[str, dict[str, Any]]
    feature_lineage: dict[str, dict[str, Any]]
    governance_gates: list[dict[str, Any]]
    execution_duration_seconds: float
    used_deterministic_response: bool
    run_id: str | None = None
    shadow_evaluation: dict[str, Any] | None = None
    stage_durations_seconds: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def build_decision_record(
    *,
    decision_id: str,
    correlation_id: str,
    scenario_name: str,
    timestamp: str,
    decision_status: str,
    config_hash: str,
    package_version: str | None,
    git_commit: str | None,
    diagnostics: Any,
    model_lineage: dict[str, "ModelLineage"],
    feature_lineage: dict[str, "FeatureLineage"],
    governance_gates: list["GateResult"],
    execution_duration_seconds: float,
    stage_durations_seconds: dict[str, float] | None = None,
    run_id: str | None = None,
    shadow_evaluation: dict[str, Any] | None = None,
) -> DecisionRecord:
    """Assemble a DecisionRecord from a completed ScenarioDiagnostics and lineage."""
    best_result = diagnostics.best_result
    used_deterministic = all(
        lineage.response_source == "deterministic_synthetic" for lineage in model_lineage.values()
    )
    return DecisionRecord(
        decision_id=decision_id,
        correlation_id=correlation_id,
        run_id=run_id,
        scenario_name=scenario_name,
        timestamp=timestamp,
        decision_status=decision_status,
        config_hash=config_hash,
        package_version=package_version,
        git_commit=git_commit,
        best_loan_rate=float(best_result.loan_rate),
        best_deposit_rate=float(best_result.deposit_rate),
        objective_value=float(best_result.total_objective),
        expected_loan_volume=float(best_result.expected_loan_volume),
        expected_deposit_volume=float(best_result.expected_deposit_volume),
        funding_shortfall=float(best_result.funding_shortfall),
        boundary_warning_flag=bool(diagnostics.boundary_diagnostics.has_warning),
        initial_boundary_warning_flag=bool(diagnostics.initial_boundary_diagnostics.has_warning),
        expansion_rounds=int(diagnostics.expansion_rounds),
        expansion_capped=bool(diagnostics.expansion_capped),
        policy_band_breached=bool(diagnostics.policy_band_breached),
        model_lineage={product: lineage.to_dict() for product, lineage in model_lineage.items()},
        feature_lineage={
            product: lineage.to_dict() for product, lineage in feature_lineage.items()
        },
        governance_gates=[gate.to_dict() for gate in governance_gates],
        execution_duration_seconds=execution_duration_seconds,
        used_deterministic_response=used_deterministic,
        shadow_evaluation=shadow_evaluation,
        stage_durations_seconds=stage_durations_seconds or {},
    )


def write_decision_record(run_dir: str | Path, record: DecisionRecord) -> Path:
    """Write decision_record.json into an existing run ledger directory.

    Additive only: never touches any other file in ``run_dir``.
    """
    path = Path(run_dir) / "decision_record.json"
    path.write_text(record.to_json(), encoding="utf-8")
    return path
