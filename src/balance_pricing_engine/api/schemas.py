"""Pydantic request/response schemas for the versioned pricing API (/v1/pricing)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_EXAMPLE_CONFIG_YAML = (
    "random_seed: 42\n"
    "simulation: {n_loan_customers: 5000, n_deposit_customers: 5000}\n"
    "market: {cash_rate: 0.0425, wholesale_funding_spread: 0.012, "
    "competitor_loan_rate: 0.0675, competitor_deposit_rate: 0.045, "
    "home_loan_reference_rate: 0.0525, term_deposit_reference_rate: 0.04}\n"
    "pricing_strategy: {home_loan_spread_bps: {min: -50, max: 50, step: 5}, "
    "term_deposit_spread_bps: {min: -40, max: 40, step: 5}}\n"
    "balance_sheet: {required_deposit_funding_ratio: 0.65, funding_shortfall_penalty_rate: 0.018}\n"
    "response: {loan_rate_elasticity: -85.0, loan_competitor_gap_elasticity: -65.0, "
    "deposit_rate_elasticity: 95.0, deposit_competitor_gap_elasticity: 75.0}\n"
)


class PricingScenarioRequestV1(BaseModel):
    """Request body for POST /v1/pricing/scenarios."""

    config_yaml: str = Field(
        ...,
        description="Full EngineConfig YAML document (same shape as configs/base.yaml).",
        examples=[_EXAMPLE_CONFIG_YAML],
    )
    scenario_name: str = Field(
        default="api_scenario",
        description="Label used in the run-ledger directory name, memo, and checklist.",
    )
    correlation_id: str | None = Field(
        default=None,
        description=(
            "Caller-supplied correlation ID for cross-system tracing. If omitted, the "
            "X-Correlation-ID request header is used, or a new UUID4 is generated."
        ),
    )
    include_sensitivity: bool = Field(
        default=False,
        description="Also run the default ±25% one-at-a-time sensitivity cases.",
    )
    persist_ledger: bool = Field(
        default=False,
        description="Persist a full governed run ledger under data/outputs/runs.",
    )
    competitor_rate_source: str | None = Field(
        default=None,
        description="Free-text provenance note for competitor rates, recorded in the ledger.",
    )
    enable_shadow_challenger: bool = Field(
        default=False,
        description=(
            "Score a compatible, non-champion challenger model against the same synthetic "
            "feature sample for comparison only. Never alters the selected rates."
        ),
    )


class GateResultSchema(BaseModel):
    code: str
    status: str
    message: str
    severity: str
    evidence: dict[str, Any]
    remediation: str | None = None


class ModelLineageSchema(BaseModel):
    product: str
    response_source: str
    model_name: str | None = None
    model_version: str | None = None
    governance_status: str | None = None
    champion_status: str | None = None
    artifact_path: str | None = None
    used_fallback: bool


class FeatureLineageSchema(BaseModel):
    feature_set_name: str
    feature_set_version: str
    schema_fingerprint: str
    product: str
    as_of: str
    row_count: int
    source: str


class PricingDecisionResponseV1(BaseModel):
    """Response body for POST /v1/pricing/scenarios.

    Idempotency: this endpoint is not idempotent. Each call produces a new
    ``decision_id`` (and, when ``persist_ledger`` is set, a new ``run_id``)
    even if the same ``correlation_id`` is reused. A future durable
    idempotency store keyed on (correlation_id, config hash) is a documented
    extension, not implemented today — see docs/deployment.md.
    """

    decision_id: str
    correlation_id: str
    status: str
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
    governance_gates: list[GateResultSchema]
    model_lineage: dict[str, ModelLineageSchema]
    feature_lineage: dict[str, FeatureLineageSchema]
    execution_duration_seconds: float
    run_id: str | None = None
    run_dir: str | None = None
    sensitivity: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Present only when the request set include_sensitivity=true: one row per "
            "sensitivity case (delta_loan_rate_bps, delta_deposit_rate_bps, "
            "delta_objective_pct, parameter_family, ...)."
        ),
    )


class ErrorResponse(BaseModel):
    """Structured error body. Never includes a raw stack trace."""

    error_code: str
    message: str
    correlation_id: str | None = None


class ReadyResponse(BaseModel):
    status: str
    config_path: str
