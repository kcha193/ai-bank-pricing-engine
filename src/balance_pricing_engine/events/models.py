"""Immutable domain events for the pricing decision lifecycle.

These events are in-process/local-audit integration seams (see
docs/adr/0004-in-process-events-before-streaming-platform.md). They carry only
small, non-sensitive summary payloads — never full synthetic customer frames or
raw configuration secrets.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_SCHEMA_VERSION = 1


def _event_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DomainEvent:
    """Common envelope fields shared by every domain event."""

    event_type: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_event_id)
    event_version: int = EVENT_SCHEMA_VERSION
    timestamp: str = field(default_factory=_now_iso)
    decision_id: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def _event(event_type: str, correlation_id: str, **payload: Any) -> DomainEvent:
    decision_id = payload.pop("decision_id", None)
    run_id = payload.pop("run_id", None)
    return DomainEvent(
        event_type=event_type,
        correlation_id=correlation_id,
        payload=payload,
        decision_id=decision_id,
        run_id=run_id,
    )


def pricing_scenario_requested(
    correlation_id: str,
    *,
    scenario_name: str,
    config_hash: str | None = None,
) -> DomainEvent:
    return _event(
        "PricingScenarioRequested",
        correlation_id,
        scenario_name=scenario_name,
        config_hash=config_hash,
    )


def pricing_decision_produced(
    correlation_id: str,
    *,
    decision_id: str,
    scenario_name: str,
    decision_status: str,
    best_loan_rate: float,
    best_deposit_rate: float,
    objective_value: float,
    run_id: str | None = None,
) -> DomainEvent:
    return _event(
        "PricingDecisionProduced",
        correlation_id,
        decision_id=decision_id,
        run_id=run_id,
        scenario_name=scenario_name,
        decision_status=decision_status,
        best_loan_rate=best_loan_rate,
        best_deposit_rate=best_deposit_rate,
        objective_value=objective_value,
    )


def governance_review_required(
    correlation_id: str,
    *,
    decision_id: str,
    scenario_name: str,
    review_reasons: list[str],
    run_id: str | None = None,
) -> DomainEvent:
    return _event(
        "GovernanceReviewRequired",
        correlation_id,
        decision_id=decision_id,
        run_id=run_id,
        scenario_name=scenario_name,
        review_reasons=list(review_reasons),
    )


def pricing_decision_blocked(
    correlation_id: str,
    *,
    decision_id: str,
    scenario_name: str,
    blocking_reasons: list[str],
    run_id: str | None = None,
) -> DomainEvent:
    return _event(
        "PricingDecisionBlocked",
        correlation_id,
        decision_id=decision_id,
        run_id=run_id,
        scenario_name=scenario_name,
        blocking_reasons=list(blocking_reasons),
    )


def model_resolved(
    correlation_id: str,
    *,
    decision_id: str,
    product: str,
    response_source: str,
    model_name: str | None,
    used_fallback: bool,
) -> DomainEvent:
    return _event(
        "ModelResolved",
        correlation_id,
        decision_id=decision_id,
        product=product,
        response_source=response_source,
        model_name=model_name,
        used_fallback=used_fallback,
    )


def run_ledger_written(
    correlation_id: str,
    *,
    decision_id: str,
    run_id: str,
    run_dir: str,
) -> DomainEvent:
    return _event(
        "RunLedgerWritten",
        correlation_id,
        decision_id=decision_id,
        run_id=run_id,
        run_dir=run_dir,
    )
