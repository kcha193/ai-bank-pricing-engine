"""Golden test: locks summary.json numeric fields for run_baseline_diagnostics(base.yaml).

Run `pytest --update-golden` to intentionally refresh after a deliberate change.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

GOLDEN_PATH = Path(__file__).parent.parent / "fixtures" / "baseline_golden.json"
TOLERANCE = 1e-6

NUMERIC_KEYS = {
    "best_loan_rate",
    "best_deposit_rate",
    "objective_value",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "capital_charge",
    "excess_deposit_income",
}
BOOL_KEYS = {
    "boundary_warning_flag",
    "initial_boundary_warning_flag",
    "expansion_capped",
    "policy_band_breached",
}
INT_KEYS = {"expansion_rounds"}


def _build_summary(diag) -> dict:
    r = diag.best_result
    return {
        "best_loan_rate": r.loan_rate,
        "best_deposit_rate": r.deposit_rate,
        "objective_value": r.total_objective,
        "capital_charge": r.capital_charge,
        "excess_deposit_income": r.excess_deposit_income,
        "expected_loan_volume": r.expected_loan_volume,
        "expected_deposit_volume": r.expected_deposit_volume,
        "funding_shortfall": r.funding_shortfall,
        "boundary_warning_flag": diag.boundary_diagnostics.has_warning,
        "initial_boundary_warning_flag": diag.initial_boundary_diagnostics.has_warning,
        "expansion_rounds": diag.expansion_rounds,
        "expansion_capped": diag.expansion_capped,
        "policy_band_breached": diag.policy_band_breached,
    }


@pytest.fixture(scope="session")
def baseline_diagnostics():
    config = load_config("configs/base.yaml")
    return run_baseline_diagnostics(config)


def test_baseline_golden(baseline_diagnostics, request):
    summary = _build_summary(baseline_diagnostics)

    if request.config.getoption("--update-golden"):
        GOLDEN_PATH.write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
        pytest.skip("golden file updated")

    assert GOLDEN_PATH.exists(), (
        "Golden file missing — run: pytest --update-golden tests/unit/test_baseline_golden.py"
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    for key in NUMERIC_KEYS:
        assert math.isclose(summary[key], golden[key], rel_tol=TOLERANCE, abs_tol=TOLERANCE), (
            f"{key}: got {summary[key]!r}, expected {golden[key]!r}"
        )
    for key in BOOL_KEYS:
        assert summary[key] == golden[key], (
            f"{key}: got {summary[key]!r}, expected {golden[key]!r}"
        )
    for key in INT_KEYS:
        assert summary[key] == golden[key], (
            f"{key}: got {summary[key]!r}, expected {golden[key]!r}"
        )
