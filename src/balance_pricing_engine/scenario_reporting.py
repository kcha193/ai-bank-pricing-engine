"""Pure scenario reporting shared by dashboard read models and offline alerts.

These summaries describe diagnostic evidence, not the full application governance gates.
They never select rates, calculate economics or grant pricing approval.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from balance_pricing_engine.competitor_rates.models import ProductType

if TYPE_CHECKING:
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics

MEDIAN_REFERENCE_LABEL = "Median-benchmark optimum (reference)"
STRATEGY_LABELS = {
    "MANUAL": "Manual competitor rate",
    "MARKET_MEDIAN": "Market median",
    "MARKET_MEAN": "Market mean",
    "MARKET_LEADER": "Market leader (lowest rate)",
    "MARKET_SECOND_LOWEST": "Second-lowest rate",
}
DEPOSIT_STRATEGY_LABELS = {
    **STRATEGY_LABELS,
    "MARKET_LEADER": "Market leader (highest rate)",
    "MARKET_SECOND_LOWEST": "Second-highest rate",
}


def strategy_label(strategy: str, product: ProductType) -> str:
    labels = DEPOSIT_STRATEGY_LABELS if product == ProductType.TERM_DEPOSIT else STRATEGY_LABELS
    return labels.get(strategy, "Unknown competitor strategy")


def diagnostic_metadata(diagnostics: ScenarioDiagnostics) -> dict[str, Any]:
    """Copy each run's own flags and volumes; preserve initial/final boundary semantics."""
    return {
        "policy_band_breached": diagnostics.policy_band_breached,
        "boundary_warning": diagnostics.boundary_diagnostics.has_warning,
        "initial_boundary_warning": diagnostics.initial_boundary_diagnostics.has_warning,
        "expansion_capped": diagnostics.expansion_capped,
        "expansion_rounds": diagnostics.expansion_rounds,
        "roe_constraint_relaxed": diagnostics.roe_constraint_relaxed,
        "funding_shortfall": diagnostics.best_result.funding_shortfall,
        "expected_loan_volume": diagnostics.best_result.expected_loan_volume,
        "expected_deposit_volume": diagnostics.best_result.expected_deposit_volume,
    }


def diagnostic_warnings(result: Any) -> tuple[str, ...]:
    """Bounded, trusted text only; missing legacy evidence is explicitly incomplete."""
    warnings = []
    flags = {
        "policy_band_breached": "Outside policy band",
        "boundary_warning": "Grid-boundary warning (final grid)",
        "expansion_capped": "Expansion cap reached",
        "roe_constraint_relaxed": "ROE floor relaxed",
    }
    incomplete = False
    for name, message in flags.items():
        value = getattr(result, name, None)
        if value is True:
            warnings.append(message)
        elif value is not False:
            incomplete = True
    for name in ("initial_boundary_warning", "expansion_rounds"):
        if getattr(result, name, None) is None:
            incomplete = True
    shortfall = getattr(result, "funding_shortfall", None)
    if shortfall is None or not math.isfinite(shortfall):
        incomplete = True
    elif shortfall > 0:
        warnings.append(f"Funding shortfall: ${shortfall:,.0f}")
    objective = getattr(result, "objective", None)
    if objective is None or not math.isfinite(objective):
        warnings.append("Objective is non-finite or missing; do not use this result")
    elif objective <= 0:
        warnings.append("Modelled objective is not positive")
    for name, side in (
        ("expected_loan_volume", "home-loan"),
        ("expected_deposit_volume", "term-deposit"),
    ):
        volume = getattr(result, name, None)
        if volume is None:
            incomplete = True
        elif not math.isfinite(volume) or volume <= 0:
            warnings.append(f"Expected {side} volume is not positive and finite")
    if incomplete:
        warnings.append("Diagnostic metadata incomplete")
    return tuple(warnings)


def diagnostic_blocked(result: Any) -> bool:
    """Preserve OBJECTIVE_FINITE's existing BLOCK severity, without running other gates."""
    objective = getattr(result, "objective", None)
    return objective is not None and not math.isfinite(objective)


def decision_quality_summary(result: Any) -> str:
    warnings = diagnostic_warnings(result)
    if warnings:
        status = "BLOCKED" if diagnostic_blocked(result) else "REVIEW REQUIRED"
        return status + " — " + "; ".join(warnings)
    return "Checked diagnostic flags clear — human review still required"


def model_best_label(result: Any) -> str:
    breach = getattr(result, "policy_band_breached", None)
    if breach is True:
        return "Model-best — outside policy band (review)"
    if breach is False:
        return "Model-best rate (within policy band)"
    return "Model-best rate (policy-band status unavailable; review)"
