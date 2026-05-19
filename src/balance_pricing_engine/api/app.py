from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError
import yaml

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


class RunScenarioRequest(BaseModel):
    config_yaml: str


app = FastAPI(title="AI Bank Pricing Engine")


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
        return {
            "best_loan_rate": float(best_result.loan_rate),
            "best_deposit_rate": float(best_result.deposit_rate),
            "objective_value": float(best_result.total_objective),
            "expected_loan_volume": float(best_result.expected_loan_volume),
            "expected_deposit_volume": float(best_result.expected_deposit_volume),
            "funding_shortfall": float(best_result.funding_shortfall),
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unexpected scenario execution error") from exc


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
