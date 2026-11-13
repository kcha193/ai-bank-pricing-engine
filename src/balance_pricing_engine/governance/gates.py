"""Typed governance gates evaluated over a completed pricing scenario.

Each gate produces a stable code, a severity, human-readable evidence, and
remediation guidance where applicable. Gates are conservative by policy (see
module docstring at the bottom for the documented policy), not
"block on every warning": most WARN-level gates surface as
``DecisionStatus.REVIEW_REQUIRED`` rather than ``BLOCKED``, preserving the
underlying optimiser recommendation for human review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from balance_pricing_engine.competitor_rates.models import ValidationIssue
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.features.contracts import FeatureLineage
    from balance_pricing_engine.governance.lineage import ModelLineage
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


class GateStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class DecisionStatus(str, Enum):
    RECOMMENDED = "RECOMMENDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class GateResult:
    code: str
    status: GateStatus
    message: str
    severity: str  # "info" | "warning" | "critical"
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status.value,
            "message": self.message,
            "severity": self.severity,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


def decision_status_from_gates(gates: list[GateResult]) -> DecisionStatus:
    """Derive the application-level decision status from evaluated gates.

    Policy: any BLOCK -> BLOCKED. Otherwise any WARN -> REVIEW_REQUIRED.
    Otherwise RECOMMENDED. This is deliberately conservative but not maximal:
    a clean interior result with complete lineage is RECOMMENDED outright.
    """
    if any(gate.status is GateStatus.BLOCK for gate in gates):
        return DecisionStatus.BLOCKED
    if any(gate.status is GateStatus.WARN for gate in gates):
        return DecisionStatus.REVIEW_REQUIRED
    return DecisionStatus.RECOMMENDED


def evaluate_gates(
    *,
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
    model_lineage: dict[str, "ModelLineage"],
    feature_lineage: dict[str, "FeatureLineage"],
    competitor_rate_issues: list["ValidationIssue"] | None = None,
) -> list[GateResult]:
    """Evaluate the full governance gate set for a completed scenario.

    ``competitor_rate_issues`` is the (possibly empty) staleness/validation
    issue list already computed by the caller when
    ``config.competitor_rates.enabled`` is true; pass ``None`` when disabled.
    """
    gates: list[GateResult] = [
        _config_validity_gate(config),
        _competitor_rate_gate(config, competitor_rate_issues),
        *_model_gates(model_lineage),
        *_feature_gates(feature_lineage),
        _boundary_warning_gate(diagnostics),
        _expansion_capped_gate(diagnostics),
        _policy_band_gate(diagnostics),
        _funding_shortfall_gate(diagnostics),
        _lineage_completeness_gate(model_lineage, feature_lineage),
        _objective_finite_gate(diagnostics),
        _objective_positive_gate(diagnostics),
        _volume_nonzero_gate(diagnostics),
        _roe_floor_gate(config, diagnostics),
    ]
    return gates


def _config_validity_gate(config: "EngineConfig") -> GateResult:
    # EngineConfig is a validated pydantic boundary; reaching this point means
    # construction already succeeded (invalid configs raise before diagnostics run).
    return GateResult(
        code="CONFIG_VALID",
        status=GateStatus.PASS,
        message="Scenario configuration passed EngineConfig validation.",
        severity="info",
        evidence={"random_seed": config.random_seed},
    )


def _competitor_rate_gate(
    config: "EngineConfig",
    issues: list["ValidationIssue"] | None,
) -> GateResult:
    competitor_rates = config.competitor_rates
    if competitor_rates is None or not competitor_rates.enabled:
        return GateResult(
            code="COMPETITOR_RATE_FRESHNESS",
            status=GateStatus.PASS,
            message="Competitor-rate ingestion is disabled; using static config rates.",
            severity="info",
        )
    issues = issues or []
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    if errors:
        return GateResult(
            code="COMPETITOR_RATE_FRESHNESS",
            status=GateStatus.BLOCK,
            message=f"Competitor-rate strict validation failed with {len(errors)} error(s).",
            severity="critical",
            evidence={"errors": [issue.message for issue in errors]},
            remediation="Re-fetch competitor rates or disable strict mode with explicit sign-off.",
        )
    if warnings:
        return GateResult(
            code="COMPETITOR_RATE_FRESHNESS",
            status=GateStatus.WARN,
            message=f"Competitor-rate validation raised {len(warnings)} warning(s).",
            severity="warning",
            evidence={"warnings": [issue.message for issue in warnings]},
            remediation="Review competitor-rate staleness/movement warnings before sign-off.",
        )
    return GateResult(
        code="COMPETITOR_RATE_FRESHNESS",
        status=GateStatus.PASS,
        message="Competitor rates passed freshness and validation checks.",
        severity="info",
    )


def _model_gates(model_lineage: dict[str, "ModelLineage"]) -> list[GateResult]:
    gates: list[GateResult] = []
    for product, lineage in model_lineage.items():
        if lineage.response_source == "deterministic_synthetic":
            gates.append(
                GateResult(
                    code=f"MODEL_APPROVAL_STATUS[{product}]",
                    status=GateStatus.PASS,
                    message=(
                        f"No approved ML champion for {product}; using the deterministic "
                        "synthetic response function (safe default)."
                    ),
                    severity="info",
                    evidence={"used_fallback": True},
                )
            )
            gates.append(
                GateResult(
                    code=f"MODEL_ARTIFACT_AVAILABLE[{product}]",
                    status=GateStatus.PASS,
                    message="No governed artifact required for the synthetic fallback path.",
                    severity="info",
                )
            )
            continue
        # response_source == "approved_ml_champion": if we reached this point, the
        # caller already resolved (and successfully loaded) the champion artifact --
        # GovernanceError from an unloadable approved artifact is raised upstream by
        # ModelScoringAgent.resolve_response_fn and is never downgraded to a gate.
        gates.append(
            GateResult(
                code=f"MODEL_APPROVAL_STATUS[{product}]",
                status=GateStatus.PASS,
                message=f"Approved champion '{lineage.model_name}' is driving {product} response.",
                severity="info",
                evidence={
                    "model_name": lineage.model_name,
                    "model_version": lineage.model_version,
                    "governance_status": lineage.governance_status,
                },
            )
        )
        gates.append(
            GateResult(
                code=f"MODEL_ARTIFACT_AVAILABLE[{product}]",
                status=GateStatus.PASS,
                message="Approved champion artifact loaded successfully.",
                severity="info",
                evidence={"artifact_path": lineage.artifact_path},
            )
        )
    return gates


def _feature_gates(feature_lineage: dict[str, "FeatureLineage"]) -> list[GateResult]:
    gates: list[GateResult] = []
    for product, lineage in feature_lineage.items():
        gates.append(
            GateResult(
                code=f"MODEL_FEATURE_COMPATIBILITY[{product}]",
                status=GateStatus.PASS,
                message=(
                    f"{product} features matched schema "
                    f"'{lineage.feature_set_name}' v{lineage.feature_set_version}."
                ),
                severity="info",
                evidence={"schema_fingerprint": lineage.schema_fingerprint},
            )
        )
    return gates


def _boundary_warning_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    if diagnostics.boundary_diagnostics.has_warning:
        return GateResult(
            code="BOUNDARY_WARNING",
            status=GateStatus.WARN,
            message="Final recommendation still sits on a searched grid boundary.",
            severity="warning",
            evidence={"warnings": list(diagnostics.boundary_diagnostics.warnings)},
            remediation="Widen the pricing grid/spread band and re-run before relying on this result.",
        )
    return GateResult(
        code="BOUNDARY_WARNING",
        status=GateStatus.PASS,
        message="Final recommendation is an interior grid optimum.",
        severity="info",
    )


def _expansion_capped_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    if diagnostics.expansion_capped:
        return GateResult(
            code="EXPANSION_CAPPED",
            status=GateStatus.WARN,
            message="Adaptive boundary expansion reached its round cap without resolving.",
            severity="warning",
            evidence={"expansion_rounds": diagnostics.expansion_rounds},
            remediation="Increase max_boundary_expansions or widen the base grid.",
        )
    return GateResult(
        code="EXPANSION_CAPPED",
        status=GateStatus.PASS,
        message="Adaptive boundary expansion did not need to reach its cap.",
        severity="info",
        evidence={"expansion_rounds": diagnostics.expansion_rounds},
    )


def _policy_band_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    if diagnostics.policy_band_breached:
        return GateResult(
            code="POLICY_BAND_BREACH",
            status=GateStatus.WARN,
            message="Recommended rate falls outside the originally configured policy band.",
            severity="warning",
            remediation="Requires explicit pricing-committee discussion before sign-off.",
        )
    return GateResult(
        code="POLICY_BAND_BREACH",
        status=GateStatus.PASS,
        message="Recommended rate is within the configured policy band.",
        severity="info",
    )


def _funding_shortfall_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    shortfall = diagnostics.best_result.funding_shortfall
    if shortfall > 0:
        return GateResult(
            code="FUNDING_SHORTFALL",
            status=GateStatus.WARN,
            message=f"Recommended rates leave a funding shortfall of ${shortfall:,.0f}.",
            severity="warning",
            evidence={"funding_shortfall": shortfall},
            remediation=(
                "Deposit funding ratio is a soft signal, not a hard constraint; confirm this "
                "is acceptable risk appetite or raise funding_shortfall_penalty_rate."
            ),
        )
    return GateResult(
        code="FUNDING_SHORTFALL",
        status=GateStatus.PASS,
        message="No funding shortfall at the recommended rates.",
        severity="info",
    )


def _lineage_completeness_gate(
    model_lineage: dict[str, "ModelLineage"],
    feature_lineage: dict[str, "FeatureLineage"],
) -> GateResult:
    missing = [p for p in ("loan", "deposit") if p not in model_lineage or p not in feature_lineage]
    if missing:
        return GateResult(
            code="LINEAGE_COMPLETE",
            status=GateStatus.WARN,
            message=f"Lineage metadata is incomplete for: {missing}.",
            severity="warning",
            remediation="Ensure model and feature resolution ran for both products.",
        )
    return GateResult(
        code="LINEAGE_COMPLETE",
        status=GateStatus.PASS,
        message="Model and feature lineage recorded for both products.",
        severity="info",
    )


def _objective_finite_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    objective = diagnostics.best_result.total_objective
    if not math.isfinite(objective):
        return GateResult(
            code="OBJECTIVE_FINITE",
            status=GateStatus.BLOCK,
            message=f"Objective value is not finite ({objective!r}); recommendation is unsafe to use.",
            severity="critical",
            remediation="Investigate the config and elasticities for degenerate values.",
        )
    return GateResult(
        code="OBJECTIVE_FINITE",
        status=GateStatus.PASS,
        message="Objective value is finite.",
        severity="info",
        evidence={"objective_value": objective},
    )


def _finite_or_none(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None when it is missing or not finite."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _objective_positive_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    objective = diagnostics.best_result.total_objective
    finite_objective = _finite_or_none(objective)
    if finite_objective is None:
        return GateResult(
            code="OBJECTIVE_POSITIVE",
            status=GateStatus.WARN,
            message=(
                f"Objective sign cannot be assessed because it is not finite ({objective!r}); "
                "see OBJECTIVE_FINITE."
            ),
            severity="warning",
            evidence={"objective_value": None},
            remediation="Resolve the OBJECTIVE_FINITE block before assessing profitability.",
        )
    if finite_objective <= 0:
        return GateResult(
            code="OBJECTIVE_POSITIVE",
            status=GateStatus.WARN,
            message=(
                f"Best modelled objective is not positive (${finite_objective:,.0f}): every "
                "evaluated candidate is loss-making or break-even under the synthetic model."
            ),
            severity="warning",
            evidence={"objective_value": finite_objective},
            remediation=(
                "Treat the selected rates as the least-bad option, not a profitable price. "
                "Review the policy band, funding inputs and calibration before sign-off."
            ),
        )
    return GateResult(
        code="OBJECTIVE_POSITIVE",
        status=GateStatus.PASS,
        message=(
            "Best modelled objective is positive under the synthetic model; this is not "
            "evidence of real-world profitability."
        ),
        severity="info",
        evidence={"objective_value": finite_objective},
    )


def _volume_nonzero_gate(diagnostics: "ScenarioDiagnostics") -> GateResult:
    best_result = diagnostics.best_result
    raw_volumes = {
        "loan": getattr(best_result, "expected_loan_volume", None),
        "deposit": getattr(best_result, "expected_deposit_volume", None),
    }
    volumes = {product: _finite_or_none(value) for product, value in raw_volumes.items()}
    # Both products are priced jointly, so both need strictly positive, finite volume.
    # A zero loan volume also leaves ROE undefined (required capital is zero).
    degenerate = [product for product, volume in volumes.items() if volume is None or volume <= 0]
    evidence = {
        "expected_loan_volume": volumes["loan"],
        "expected_deposit_volume": volumes["deposit"],
        "degenerate_products": degenerate,
    }
    if degenerate:
        return GateResult(
            code="VOLUME_NONZERO",
            status=GateStatus.WARN,
            message=(
                "Expected volume is zero, negative, non-finite or missing for: "
                f"{', '.join(degenerate)}. The recommended rate for that product is degenerate."
            ),
            severity="warning",
            evidence=evidence,
            remediation=(
                "Check the response elasticities, rate grid and customer simulation; a "
                "rate that attracts no business is not a usable price."
            ),
        )
    return GateResult(
        code="VOLUME_NONZERO",
        status=GateStatus.PASS,
        message="Expected loan and deposit volumes are both positive and finite.",
        severity="info",
        evidence=evidence,
    )


def _roe_floor_gate(config: "EngineConfig", diagnostics: "ScenarioDiagnostics") -> GateResult:
    floor = config.balance_sheet.min_return_on_equity
    relaxed = getattr(diagnostics, "roe_constraint_relaxed", None)
    selected_roe = _finite_or_none(getattr(diagnostics, "selected_return_on_equity", None))
    evidence = {
        "min_return_on_equity": floor,
        "selected_return_on_equity": selected_roe,
        "roe_constraint_relaxed": relaxed if isinstance(relaxed, bool) else None,
    }
    if floor is None:
        return GateResult(
            code="ROE_FLOOR_MET",
            status=GateStatus.PASS,
            message="No minimum return on equity is configured.",
            severity="info",
            evidence=evidence,
        )
    if relaxed is True:
        return GateResult(
            code="ROE_FLOOR_MET",
            status=GateStatus.WARN,
            message=(
                f"No evaluated candidate met the configured minimum return on equity "
                f"{floor:.2%}; the optimiser fell back to the unconstrained "
                "highest-objective candidate."
            ),
            severity="warning",
            evidence=evidence,
            remediation=(
                "The ROE hurdle is not met by this recommendation. Lower or justify the "
                "floor, widen the grid, or reject the result at pricing committee."
            ),
        )
    if relaxed is not False or selected_roe is None:
        return GateResult(
            code="ROE_FLOOR_MET",
            status=GateStatus.WARN,
            message=(
                f"A minimum return on equity of {floor:.2%} is configured, but the ROE "
                "evidence for the selected result is missing or not finite."
            ),
            severity="warning",
            evidence=evidence,
            remediation="Re-run the scenario through run_baseline_diagnostics to record ROE.",
        )
    if selected_roe < floor:
        return GateResult(
            code="ROE_FLOOR_MET",
            status=GateStatus.WARN,
            message=(
                f"Selected return on equity {selected_roe:.2%} is below the configured "
                f"minimum {floor:.2%}, although the floor was not reported as relaxed."
            ),
            severity="warning",
            evidence=evidence,
            remediation="Investigate the inconsistent ROE evidence before sign-off.",
        )
    return GateResult(
        code="ROE_FLOOR_MET",
        status=GateStatus.PASS,
        message=(
            f"Selected return on equity {selected_roe:.2%} meets the configured minimum "
            f"{floor:.2%}."
        ),
        severity="info",
        evidence=evidence,
    )
