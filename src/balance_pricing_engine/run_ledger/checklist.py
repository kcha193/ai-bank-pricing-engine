"""Governance checklist generator for the pricing run ledger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
    json_content = _build_checklist_json(metadata, config, diagnostics)
    (run_dir / "governance_checklist.json").write_text(json_content, encoding="utf-8")


def _build_checklist_json(
    metadata: "RunMetadata",
    config: "EngineConfig | None" = None,
    diagnostics: "ScenarioDiagnostics | None" = None,
) -> str:
    pkg = metadata.package_version or "unknown"
    boundary_status = "REQUIRES REVIEW" if metadata.boundary_warning_flag else "CLEAR"
    optimizer_details = [
        f"Status: {boundary_status}",
        f"Initial boundary warning: {'Yes' if metadata.initial_boundary_warning_flag else 'No'}",
        f"Expansion rounds completed: {metadata.expansion_rounds}",
        f"Post-expansion boundary warning: {'Yes' if metadata.boundary_warning_flag else 'No'}",
        f"Policy band breached: {'Yes' if metadata.policy_band_breached else 'No'}",
    ]
    sensitivity_details = [
        "Confirm no single-factor scenario reverses the recommendation without explanation."
    ]
    competitor_details: list[str] = []
    funding_details: list[str] = []
    elasticity_details: list[str] = []
    if config is not None:
        m = config.market
        bs = config.balance_sheet
        r = config.response
        competitor_details.append(
            "Verify competitor rates reflect current published rates "
            f"(loan: {m.competitor_loan_rate:.2%}, deposit: {m.competitor_deposit_rate:.2%})."
        )
        if metadata.competitor_rate_source:
            competitor_details.append(f"Source: {metadata.competitor_rate_source}")
        funding_details.append(
            "Required deposit funding ratio "
            f"({bs.required_deposit_funding_ratio:.0%}) and penalty rate "
            f"({bs.funding_shortfall_penalty_rate:.2%}) reflect current risk appetite."
        )
        elasticity_details.append(
            "Loan rate elasticity "
            f"({r.loan_rate_elasticity:.0f}), deposit rate elasticity "
            f"({r.deposit_rate_elasticity:.0f}), loan competitor gap "
            f"({r.loan_competitor_gap_elasticity:.0f}), and deposit competitor gap "
            f"({r.deposit_competitor_gap_elasticity:.0f}) are consistent with market intelligence."
        )
    if diagnostics is not None:
        optimizer_details.append(
            f"Best rate pair: loan {diagnostics.best_result.loan_rate:.2%}, "
            f"deposit {diagnostics.best_result.deposit_rate:.2%}."
        )
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
                "details": optimizer_details,
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "sensitivity-reviewed",
                "section": "Optimizer Diagnostics",
                "label": "Sensitivity reviewed",
                "status": "pending",
                "details": sensitivity_details,
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "competitor-rate-freshness",
                "section": "Market Assumptions",
                "label": "Competitor-rate freshness reviewed",
                "status": "pending",
                "details": competitor_details,
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "funding-assumptions-reviewed",
                "section": "Market Assumptions",
                "label": "Funding assumptions reviewed",
                "status": "pending",
                "details": funding_details,
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "elasticity-assumptions-reviewed",
                "section": "Model Assumptions",
                "label": "Elasticity assumptions reviewed",
                "status": "pending",
                "details": elasticity_details,
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "conduct-fairness-noted",
                "section": "Model Assumptions",
                "label": "Conduct and fairness limitations noted",
                "status": "pending",
                "details": [
                    "Portfolio-level pricing does not account for customer-level fairness or "
                    "conduct obligations. Any move toward segmented pricing requires a separate "
                    "governance review."
                ],
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "human-approval",
                "section": "Approval",
                "label": "Human approval required",
                "status": "pending",
                "details": [
                    "This recommendation must not be implemented without explicit sign-off from "
                    "an authorised pricing decision-maker."
                ],
                "notes": None,
                "signed_by": None,
                "signed_at": None,
            },
            {
                "id": "synthetic-data-acknowledged",
                "section": "Approval",
                "label": "Synthetic-data limitation acknowledged",
                "status": "pending",
                "details": [
                    "All results are based on synthetic customer populations and must not be "
                    "treated as production pricing decisions."
                ],
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


_VALID_SIGN_OFF_STATUSES = {"approved", "rejected", "waived"}


def sign_off_item(
    run_dir: Path,
    item_id: str,
    approver: str,
    *,
    notes: str | None = None,
    status: str = "approved",
) -> None:
    """Mark one checklist item as signed off; update JSON and regenerate markdown."""
    if status not in _VALID_SIGN_OFF_STATUSES:
        raise ValueError(
            f"Invalid sign-off status '{status}'. Must be one of: {sorted(_VALID_SIGN_OFF_STATUSES)}"
        )
    json_path = Path(run_dir) / "governance_checklist.json"
    if not json_path.exists():
        raise FileNotFoundError(f"governance_checklist.json not found in {run_dir}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    matched = [item for item in payload["items"] if item["id"] == item_id]
    if not matched:
        raise ValueError(
            f"Checklist item '{item_id}' not found. "
            f"Known IDs: {[i['id'] for i in payload['items']]}"
        )
    signed_at = datetime.now(timezone.utc).isoformat()
    for item in matched:
        item["status"] = status
        item["signed_by"] = approver
        item["signed_at"] = signed_at
        if notes is not None:
            item["notes"] = notes
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    _rebuild_markdown_from_json(Path(run_dir), payload)


def sign_off_all_items(
    run_dir: Path,
    approver: str,
    *,
    notes: str | None = None,
    status: str = "approved",
) -> None:
    """Mark every checklist item as signed off; update JSON and regenerate markdown."""
    if status not in _VALID_SIGN_OFF_STATUSES:
        raise ValueError(
            f"Invalid sign-off status '{status}'. Must be one of: {sorted(_VALID_SIGN_OFF_STATUSES)}"
        )
    json_path = Path(run_dir) / "governance_checklist.json"
    if not json_path.exists():
        raise FileNotFoundError(f"governance_checklist.json not found in {run_dir}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    signed_at = datetime.now(timezone.utc).isoformat()
    for item in payload["items"]:
        item["status"] = status
        item["signed_by"] = approver
        item["signed_at"] = signed_at
        if notes is not None:
            item["notes"] = notes
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    _rebuild_markdown_from_json(Path(run_dir), payload)


def _rebuild_markdown_from_json(run_dir: Path, payload: dict) -> None:
    """Regenerate governance_checklist.md from an already-loaded JSON payload."""
    lines = [
        f"# Governance Checklist — {payload['scenario_name']}",
        "",
        f"Run: `{payload['run_id']}`  ",
        f"Timestamp: {payload['timestamp']}  ",
        f"Config hash: `{payload['config_hash']}`  ",
        "",
        "All items must be reviewed and signed off before this run's recommendation is accepted.",
        "",
        "---",
        "",
    ]
    current_section: str | None = None
    for item in payload["items"]:
        section = item.get("section", "")
        if section != current_section:
            if current_section is not None:
                lines.extend(["", "---", ""])
            lines.extend([f"## {section}", ""])
            current_section = section

        status = item["status"]
        check = "x" if status == "approved" else " "
        label = item["label"]
        signed_by = item.get("signed_by") or ""
        signed_at = item.get("signed_at") or ""
        notes_text = item.get("notes") or ""

        line = f"- [{check}] **{label}** — status: {status}"
        if signed_by:
            line += f" | signed by: {signed_by} at {signed_at}"
        if notes_text:
            line += f"\n  - Notes: {notes_text}"
        lines.append(line)
        for detail in item.get("details", []):
            lines.append(f"  - {detail}")
        lines.append("")

    lines.append(f"_Generated by {payload.get('generated_by', 'ai-bank-pricing-engine')}_")
    (run_dir / "governance_checklist.md").write_text("\n".join(lines), encoding="utf-8")
