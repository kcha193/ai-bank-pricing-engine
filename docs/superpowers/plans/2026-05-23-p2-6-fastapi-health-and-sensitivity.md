# P2-6: FastAPI Health Endpoint + Optional Sensitivity Response

> **STATUS: ✅ COMPLETE** — FastAPI /health + include_sensitivity — PR #36

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /health` to the FastAPI app and extend `POST /run-scenario` to optionally return sensitivity analysis data when the caller passes `include_sensitivity: true` in the request body.

**Architecture:** `RunScenarioRequest` gains an optional `include_sensitivity: bool = False` field. When `True`, `run_sensitivity_analysis(config)` is called after the main scenario run, and the serialised sensitivity rows are appended to the response under a `sensitivity` key. `run_sensitivity_analysis` takes only `config` — it re-generates its own customers internally. A new `GET /health` endpoint returns engine version and status. No new dependencies — sensitivity analysis is already in `diagnostics.py`.

**Tech Stack:** Python 3.9+, FastAPI (already installed), httpx + TestClient (already installed), pytest.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/api/app.py` | Add `/health`, extend `RunScenarioRequest`, add `_sensitivity_payload` |
| Modify | `tests/unit/test_api.py` | Tests for `/health` and `include_sensitivity` |

---

## Background: relevant imports

`run_sensitivity_analysis` lives in `diagnostics.py`:

```python
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
```

Its signature:

```python
def run_sensitivity_analysis(
    config: EngineConfig,
    cases: Sequence[SensitivityCase] | None = None,
    max_boundary_expansions: int = 2,
    n_seeds: int = 1,
) -> pd.DataFrame:
```

It re-generates synthetic customers internally — no customers or market state need to be passed from `ScenarioDiagnostics`.

---

## Task 1: `GET /health`

**Files:**
- Modify: `src/balance_pricing_engine/api/app.py`
- Test: `tests/unit/test_api.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_api.py`:

```python
def test_health_returns_200_with_status_ok():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload
```

- [ ] **Step 2: Run to confirm it fails**

```bash
python3 -m pytest tests/unit/test_api.py::test_health_returns_200_with_status_ok -v
```

Expected: `404 Not Found`.

- [ ] **Step 3: Add `GET /health` to `app.py`**

Add this import at the top of `app.py` (after existing imports):

```python
from importlib.metadata import PackageNotFoundError, version as _pkg_version
```

Add the endpoint after the `app = FastAPI(...)` line:

```python
@app.get("/health")
def health() -> dict[str, str]:
    try:
        pkg_version = _pkg_version("ai-bank-pricing-engine")
    except PackageNotFoundError:
        pkg_version = "unknown"
    return {"status": "ok", "version": pkg_version}
```

- [ ] **Step 4: Run the test**

```bash
python3 -m pytest tests/unit/test_api.py::test_health_returns_200_with_status_ok -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/api/app.py tests/unit/test_api.py
git commit -m "feat: add GET /health endpoint to FastAPI app"
```

---

## Task 2: Optional sensitivity data in `POST /run-scenario`

**Files:**
- Modify: `src/balance_pricing_engine/api/app.py`
- Test: `tests/unit/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_api.py`:

```python
def test_run_scenario_without_sensitivity_flag_has_no_sensitivity_key():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post("/run-scenario", json={"config_yaml": config_yaml})

    assert response.status_code == 200
    assert "sensitivity" not in response.json()


def test_run_scenario_with_include_sensitivity_false_has_no_sensitivity_key():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post(
        "/run-scenario", json={"config_yaml": config_yaml, "include_sensitivity": False}
    )

    assert response.status_code == 200
    assert "sensitivity" not in response.json()


def test_run_scenario_with_include_sensitivity_true_returns_sensitivity_rows():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post(
        "/run-scenario", json={"config_yaml": config_yaml, "include_sensitivity": True}
    )

    assert response.status_code == 200
    payload = response.json()
    assert "sensitivity" in payload
    rows = payload["sensitivity"]
    assert isinstance(rows, list)
    assert len(rows) > 0
    first = rows[0]
    assert "scenario_name" in first
    assert "delta_objective_pct" in first
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_api.py::test_run_scenario_with_include_sensitivity_true_returns_sensitivity_rows -v
```

Expected: FAIL — `sensitivity` key not in response.

- [ ] **Step 3: Update `app.py`**

Replace the full content of `src/balance_pricing_engine/api/app.py` with:

```python
from __future__ import annotations

import math
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError
import yaml

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


class RunScenarioRequest(BaseModel):
    config_yaml: str
    include_sensitivity: bool = False


app = FastAPI(title="AI Bank Pricing Engine")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        pkg_version = _pkg_version("ai-bank-pricing-engine")
    except PackageNotFoundError:
        pkg_version = "unknown"
    return {"status": "ok", "version": pkg_version}


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
```

- [ ] **Step 4: Run all API tests**

```bash
python3 -m pytest tests/unit/test_api.py -v
```

Expected: all 6 tests pass (3 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/api/app.py tests/unit/test_api.py
git commit -m "feat: optional sensitivity payload and health endpoint in FastAPI"
```

---

## Self-review

**Spec coverage:**
- `GET /health` returns `status` + `version` ✓
- `POST /run-scenario` with `include_sensitivity: false` (or omitted) is unchanged ✓
- `POST /run-scenario` with `include_sensitivity: true` returns `sensitivity` rows ✓
- Tests cover all three new behaviours ✓

**Placeholder scan:** Step 4 flags the signature verification explicitly rather than hardcoding it.

**Type consistency:** `RunScenarioRequest.include_sensitivity: bool = False`; `payload["sensitivity"]` is `list[dict]` from `.to_dict(orient="records")`.
