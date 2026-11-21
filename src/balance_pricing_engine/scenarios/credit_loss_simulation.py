"""Explicit opt-in annual mortgage loss simulation over seeded prospects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

from pydantic import ConfigDict, Field, field_serializer, model_validator

from balance_pricing_engine.config import EconomicConfig, EngineConfig, revalidation_payload
from balance_pricing_engine.config_identity import canonical_config_bytes
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.pricing.credit_loss import (
    ANNUAL_EXPECTED_LOSS_COLUMN,
    CreditLossEstimate,
    CreditLossProfile,
)
from balance_pricing_engine.scenarios.runner import (
    ScenarioDiagnostics,
    _evaluate_pricing_population,
    _generate_pricing_population,
)


class _CreditLossSimulationInputs(EconomicConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_config: EngineConfig
    max_boundary_expansions: int = Field(default=2, ge=0)
    loss_model: Literal["pd_lgd_ead"] = "pd_lgd_ead"

    @field_serializer("engine_config")
    def _serialize_engine_config(self, engine: EngineConfig) -> dict:
        # Preserve defaults whose validation depends on whether the caller supplied them.
        return revalidation_payload(engine)

    @model_validator(mode="after")
    def require_static_market(self) -> _CreditLossSimulationInputs:
        rates = self.engine_config.competitor_rates
        if rates is not None and rates.enabled:
            raise ValueError("Credit loss simulations require static market inputs.")
        return self


class CreditLossSimulationConfig(_CreditLossSimulationInputs):
    """Validated simulation inputs, deliberately outside the governed pricing schema."""

    profile: CreditLossProfile


@dataclass(frozen=True)
class CreditLossSimulation:
    """Synthetic estimates, pricing diagnostics and content-addressed profile evidence."""

    diagnostics: ScenarioDiagnostics
    loss_estimate: CreditLossEstimate
    effective_config_json: bytes
    config_hash: str
    governance_status: DecisionStatus

    @property
    def effective_config(self) -> CreditLossSimulationConfig:
        """Return a fresh view of the config bound to the reported hash."""
        return CreditLossSimulationConfig.model_validate_json(self.effective_config_json)


def run_credit_loss_simulation(config: CreditLossSimulationConfig) -> CreditLossSimulation:
    """Evaluate an illustrative profile with the joint or segmented pricing engine."""
    config = CreditLossSimulationConfig.model_validate(config)
    effective_config_json = canonical_config_bytes(config)
    engine = config.engine_config
    loans, deposits = _generate_pricing_population(engine)
    estimate = config.profile.estimate(loans["loan_amount"], loans.get("lvr"))
    loans[ANNUAL_EXPECTED_LOSS_COLUMN] = estimate.annual_expected_loss_nzd
    diagnostics = replace(
        _evaluate_pricing_population(
            engine, loans, deposits, max_boundary_expansions=config.max_boundary_expansions
        ),
        credit_loss_profile=config.profile,
    )
    gates = evaluate_gates(
        config=engine, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    return CreditLossSimulation(
        diagnostics=diagnostics,
        loss_estimate=estimate,
        effective_config_json=effective_config_json,
        config_hash=sha256(effective_config_json).hexdigest(),
        governance_status=decision_status_from_gates(gates),
    )
