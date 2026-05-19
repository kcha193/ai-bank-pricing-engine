"""Governance checklist generator for the pricing run ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.run_ledger.metadata import RunMetadata
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def write_governance_checklist(
    run_dir: Path,
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
) -> None:
    """Write governance_checklist.md into run_dir."""
    content = _build_checklist(metadata, config, diagnostics)
    (run_dir / "governance_checklist.md").write_text(content, encoding="utf-8")
    json_content = _build_checklist_json(metadata)
    (run_dir / "governance_checklist.json").write_text(json_content, encoding="utf-8")


def _build_checklist_json(metadata: "RunMetadata") -> str:
    pkg = metadata.package_version or "unknown"
    payload = {
        "run_id": metadata.run_id,
        "scenario_name": metadata.scenario_name,
        "timestamp": metadata.timestamp,
        "config_hash": metadata.config_hash,
        "generated_by": f"ai-bank-pricing-engine v{pkg}",
        "items": [
            {
                "id": "boundary-result-reviewed",
                "section": "Optimizer Diagnostics",
                "label": "Boundary result reviewed",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "sensitivity-reviewed",
                "section": "Optimizer Diagnostics",
                "label": "Sensitivity reviewed",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "competitor-rate-freshness",
                "section": "Market Assumptions",
                "label": "Competitor-rate freshness reviewed",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "funding-assumptions-reviewed",
                "section": "Market Assumptions",
                "label": "Funding assumptions reviewed",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "elasticity-assumptions-reviewed",
                "section": "Model Assumptions",
                "label": "Elasticity assumptions reviewed",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "conduct-fairness-noted",
                "section": "Model Assumptions",
                "label": "Conduct and fairness limitations noted",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "human-approval",
                "section": "Approval",
                "label": "Human approval required",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "synthetic-data-acknowledged",
                "section": "Approval",
                "label": "Synthetic-data limitation acknowledged",
                "status": "pending",
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _build_checklist(
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
) -> str:
    boundary_status = "REQUIRES REVIEW" if metadata.boundary_warning_flag else "CLEAR"
    m = config.market
    r = config.response
    bs = config.balance_sheet
    source_note = (
        f"\n  - Source: {metadata.competitor_rate_source}"
        if metadata.competitor_rate_source
        else ""
    )
    pkg = metadata.package_version or "unknown"
    lines = [
        f"# Governance Checklist — {metadata.scenario_name}",
        "",
        f"Run: `{metadata.run_id}`  ",
        f"Timestamp: {metadata.timestamp}  ",
        f"Config hash: `{metadata.config_hash}`  ",
        "",
        "All items must be reviewed and signed off before this run's recommendation is accepted.",
        "",
        "---",
        "",
        "## Optimizer Diagnostics",
        "",
        f"- [ ] **Boundary result reviewed** — Status: {boundary_status}",
        f"  - Initial boundary warning: {'Yes' if metadata.initial_boundary_warning_flag else 'No'}",
        f"  - Expansion rounds completed: {metadata.expansion_rounds}",
        f"  - Post-expansion boundary warning: {'Yes' if metadata.boundary_warning_flag else 'No'}",
        f"  - Policy band breached: {'Yes' if metadata.policy_band_breached else 'No'}",
        "",
        "- [ ] **Sensitivity reviewed** — Confirm no single-factor scenario reverses the "
        "recommendation without explanation.",
        "",
        "---",
        "",
        "## Market Assumptions",
        "",
        "- [ ] **Competitor-rate freshness reviewed** — Verify competitor rates reflect current "
        f"published rates (loan: {m.competitor_loan_rate:.2%}, deposit: {m.competitor_deposit_rate:.2%})."
        f"{source_note}",
        "",
        "- [ ] **Funding assumptions reviewed** — Required deposit funding ratio "
        f"({bs.required_deposit_funding_ratio:.0%}) and penalty rate "
        f"({bs.funding_shortfall_penalty_rate:.2%}) reflect current risk appetite.",
        "",
        "---",
        "",
        "## Model Assumptions",
        "",
        "- [ ] **Elasticity assumptions reviewed** — Loan rate elasticity "
        f"({r.loan_rate_elasticity:.0f}), deposit rate elasticity ({r.deposit_rate_elasticity:.0f}), "
        f"loan competitor gap ({r.loan_competitor_gap_elasticity:.0f}), and deposit competitor gap "
        f"({r.deposit_competitor_gap_elasticity:.0f}) are consistent with market intelligence.",
        "",
        "- [ ] **Conduct and fairness limitations noted** — Portfolio-level pricing does not "
        "account for customer-level fairness or conduct obligations. Any move toward segmented "
        "pricing requires a separate governance review.",
        "",
        "---",
        "",
        "## Approval",
        "",
        "- [ ] **Human approval required** — This recommendation must not be implemented without "
        "explicit sign-off from an authorised pricing decision-maker.",
        "",
        "- [ ] **Synthetic-data limitation acknowledged** — All results are based on synthetic "
        "customer populations and must not be treated as production pricing decisions.",
        "",
        "---",
        "",
        f"_Generated by ai-bank-pricing-engine v{pkg} on {metadata.timestamp}_",
    ]
    return "\n".join(lines)
