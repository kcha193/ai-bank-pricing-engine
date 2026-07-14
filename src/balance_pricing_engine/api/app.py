from __future__ import annotations

import math
import os
import tempfile
import uuid
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
import yaml

from balance_pricing_engine.api.schemas import (
    ErrorResponse,
    FeatureLineageSchema,
    GateResultSchema,
    ModelLineageSchema,
    PricingDecisionResponseV1,
    PricingScenarioRequestV1,
    ReadyResponse,
)
from balance_pricing_engine.application.contracts import (
    PricingDecision,
    PricingScenarioRequest as ApplicationPricingScenarioRequest,
)
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

CORRELATION_ID_HEADER = "X-Correlation-ID"
DEFAULT_CONFIG_PATH_ENV = "PRICING_ENGINE_CONFIG_PATH"


class RunScenarioRequest(BaseModel):
    config_yaml: str
    include_sensitivity: bool = False


app = FastAPI(
    title="AI Bank Pricing Engine",
    description=(
        "Synthetic retail bank balance-sheet pricing engine. POST /run-scenario is the "
        "original, stable endpoint. POST /v1/pricing/scenarios is the versioned, "
        "governed endpoint backed by the application PricingService — prefer it for new "
        "integrations. Authentication/authorisation is a documented production boundary, "
        "not implemented here; see docs/deployment.md."
    ),
)
_pricing_service = PricingService()


@app.get("/health")
def health() -> dict[str, str]:
    try:
        pkg_version = _pkg_version("ai-bank-pricing-engine")
    except PackageNotFoundError:
        pkg_version = "unknown"
    return {"status": "ok", "version": pkg_version}


@app.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ErrorResponse}},
)
def ready() -> ReadyResponse:
    """Readiness probe: confirms the default engine config is loadable.

    Container orchestrators should treat a 503 here as "do not route traffic
    yet" rather than restarting the process — see docs/deployment.md.
    """
    config_path = os.environ.get(DEFAULT_CONFIG_PATH_ENV, "configs/base.yaml")
    try:
        load_config(config_path)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=_error_detail("CONFIG_UNAVAILABLE", str(exc), correlation_id=None),
        ) from exc
    return ReadyResponse(status="ready", config_path=config_path)


@app.post(
    "/v1/pricing/scenarios",
    response_model=PricingDecisionResponseV1,
    tags=["pricing"],
    summary="Run a governed pricing scenario",
    responses={
        422: {"model": ErrorResponse, "description": "Config validation or unsupported scenario"},
        503: {"model": ErrorResponse, "description": "Governed model artifact unavailable"},
        500: {"model": ErrorResponse, "description": "Unexpected server error"},
    },
)
def create_pricing_scenario(
    scenario_request: PricingScenarioRequestV1,
    request: Request,
    response: Response,
) -> PricingDecisionResponseV1:
    """Governed pricing decision: validation -> feature/model resolution -> optimisation
    -> governance gates -> decision record -> optional ledger persistence.

    Not idempotent (see ``PricingDecisionResponseV1`` docstring). Distinguishes
    validation errors (422), governed-model artifact failures (503), and
    unexpected errors (500); never returns a raw stack trace.
    """
    correlation_id = (
        scenario_request.correlation_id
        or request.headers.get(CORRELATION_ID_HEADER)
        or str(uuid.uuid4())
    )
    response.headers[CORRELATION_ID_HEADER] = correlation_id

    try:
        raw_config = yaml.safe_load(scenario_request.config_yaml)
        config = EngineConfig(**raw_config)
    except (TypeError, ValidationError, yaml.YAMLError) as exc:
        raise HTTPException(
            status_code=422,
            detail=_error_detail("VALIDATION_ERROR", str(exc), correlation_id),
        ) from exc

    config_path = _materialize_config_yaml(scenario_request.config_yaml)
    try:
        service_request = ApplicationPricingScenarioRequest(
            config=config,
            scenario_name=scenario_request.scenario_name,
            config_path=config_path,
            correlation_id=correlation_id,
            include_sensitivity=scenario_request.include_sensitivity,
            persist_ledger=scenario_request.persist_ledger,
            competitor_rate_source=scenario_request.competitor_rate_source,
            enable_shadow_challenger=scenario_request.enable_shadow_challenger,
        )
        try:
            decision = _pricing_service.run_scenario(service_request)
        except GovernanceError as exc:
            raise HTTPException(
                status_code=503,
                detail=_error_detail("MODEL_ARTIFACT_UNAVAILABLE", str(exc), correlation_id),
            ) from exc
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=422,
                detail=_error_detail("UNSUPPORTED_SCENARIO", str(exc), correlation_id),
            ) from exc
        except (ValueError, FileNotFoundError) as exc:
            # PricingService.run_scenario applies competitor rates before optimisation;
            # a stale/failed/missing strict competitor-rate source raises here (see
            # apply_competitor_rates_to_config), not a server-side failure.
            raise HTTPException(
                status_code=422,
                detail=_error_detail("COMPETITOR_RATE_VALIDATION_ERROR", str(exc), correlation_id),
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=_error_detail(
                    "INTERNAL_ERROR", "Unexpected scenario execution error", correlation_id
                ),
            ) from exc
    finally:
        config_path.unlink(missing_ok=True)

    return _decision_to_response(decision)


def _error_detail(error_code: str, message: str, correlation_id: str | None) -> dict[str, Any]:
    return {"error_code": error_code, "message": message, "correlation_id": correlation_id}


def _materialize_config_yaml(config_yaml: str) -> Path:
    tmp_dir = Path(tempfile.gettempdir()) / "bank_pricing_api_configs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.yaml"
    tmp_path.write_text(config_yaml, encoding="utf-8")
    return tmp_path


def _decision_to_response(decision: PricingDecision) -> PricingDecisionResponseV1:
    return PricingDecisionResponseV1(
        decision_id=decision.decision_id,
        correlation_id=decision.correlation_id,
        status=decision.status.value,
        scenario_name=decision.scenario_name,
        best_loan_rate=decision.best_loan_rate,
        best_deposit_rate=decision.best_deposit_rate,
        objective_value=decision.objective_value,
        expected_loan_volume=decision.expected_loan_volume,
        expected_deposit_volume=decision.expected_deposit_volume,
        funding_shortfall=decision.funding_shortfall,
        boundary_warning_flag=decision.boundary_warning_flag,
        initial_boundary_warning_flag=decision.initial_boundary_warning_flag,
        expansion_rounds=decision.expansion_rounds,
        expansion_capped=decision.expansion_capped,
        policy_band_breached=decision.policy_band_breached,
        governance_gates=[GateResultSchema(**gate.to_dict()) for gate in decision.governance.gates],
        model_lineage={
            product: ModelLineageSchema(**lineage.to_dict())
            for product, lineage in decision.model_lineage.items()
        },
        feature_lineage={
            product: FeatureLineageSchema(**lineage.to_dict())
            for product, lineage in decision.feature_lineage.items()
        },
        execution_duration_seconds=decision.execution_duration_seconds,
        run_id=decision.run_id,
        run_dir=str(decision.run_dir) if decision.run_dir else None,
    )


@app.post("/run-scenario")
def run_scenario(request: RunScenarioRequest) -> dict[str, Any]:
    try:
        raw_config = yaml.safe_load(request.config_yaml)
        config = EngineConfig(**raw_config)
    except (TypeError, ValidationError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        diagnostics = run_baseline_diagnostics(config)
        best_result = diagnostics.best_result
        payload: dict[str, Any] = {
            "best_loan_rate": float(best_result.loan_rate),
            "best_deposit_rate": float(best_result.deposit_rate),
            "objective_value": float(best_result.total_objective),
            "expected_loan_volume": float(best_result.expected_loan_volume),
            "expected_deposit_volume": float(best_result.expected_deposit_volume),
            "funding_shortfall": float(best_result.funding_shortfall),
            "capital_charge": float(best_result.capital_charge),
            "excess_deposit_income": float(best_result.excess_deposit_income),
            "boundary_warning_flag": bool(diagnostics.boundary_diagnostics.has_warning),
            "initial_boundary_warning_flag": bool(
                diagnostics.initial_boundary_diagnostics.has_warning
            ),
            "expansion_rounds": int(diagnostics.expansion_rounds),
            "expansion_capped": bool(diagnostics.expansion_capped),
            "policy_band_breached": bool(diagnostics.policy_band_breached),
            "top_results": _top_results_payload(diagnostics),
        }
        if request.include_sensitivity:
            sensitivity_frame = run_sensitivity_analysis(config)
            payload["sensitivity"] = sensitivity_frame.to_dict(orient="records")
    except HTTPException:
        raise
    except NotImplementedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unexpected scenario execution error") from exc

    return payload


def _top_results_payload(diagnostics: Any) -> list[dict[str, float | None]]:
    top_results = diagnostics.top_results(5).loc[:, GRID_RESULT_COLUMNS]
    records = top_results.where(top_results.notna(), None).to_dict(orient="records")
    return [
        {column: _json_float(row[column]) for column in GRID_RESULT_COLUMNS}
        for row in records
    ]


def _json_float(value: Any) -> float | None:
    if value is None:
        return None
    float_value = float(value)
    if math.isnan(float_value):
        return None
    return float_value
