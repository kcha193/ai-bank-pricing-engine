"""Pricing memo generator for the pricing run ledger."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.run_ledger.metadata import RunMetadata
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def write_pricing_memo(
    run_dir: Path,
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
    sensitivity: Optional[pd.DataFrame] = None,
) -> None:
    """Write pricing_memo.md into run_dir."""
    content = _build_memo(metadata, config, diagnostics, sensitivity)
    (run_dir / "pricing_memo.md").write_text(content, encoding="utf-8")


def _build_memo(
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
    sensitivity: Optional[pd.DataFrame],
) -> str:
    sections = [
        _header(metadata),
        _recommendation(diagnostics, config),
        _objective_decomposition(diagnostics),
        _funding_position(diagnostics),
        _sensitivity_section(sensitivity),
        _boundary_section(diagnostics),
        _model_risk_notes(),
        _committee_questions(config, diagnostics),
        _synthetic_data_limitation(),
    ]
    return "\n\n".join(sections)


def _header(metadata: "RunMetadata") -> str:
    git = metadata.git_commit or "n/a"
    source = metadata.competitor_rate_source or "n/a"
    return (
        f"# Pricing Memo — {metadata.scenario_name}\n\n"
        f"Run: `{metadata.run_id}`  \n"
        f"Timestamp: {metadata.timestamp}  \n"
        f"Config hash: `{metadata.config_hash}`  \n"
        f"Git commit: {git}  \n"
        f"Competitor rate source: {source}"
    )


def _recommendation(
    diagnostics: "ScenarioDiagnostics",
    config: "EngineConfig",
) -> str:
    r = diagnostics.best_result
    ref_loan = config.market.home_loan_reference_rate
    ref_dep = config.market.term_deposit_reference_rate
    loan_spread = (
        f"{r.home_loan_spread_to_market_bps:.0f} bps" if ref_loan is not None else "n/a"
    )
    dep_spread = (
        f"{r.term_deposit_spread_to_market_bps:.0f} bps" if ref_dep is not None else "n/a"
    )
    return (
        "## Recommendation\n\n"
        "| Rate | Value |\n"
        "|------|-------|\n"
        f"| Home loan rate | {r.loan_rate:.2%} |\n"
        f"| Term deposit rate | {r.deposit_rate:.2%} |\n"
        f"| Home loan spread to market | {loan_spread} |\n"
        f"| Term deposit spread to market | {dep_spread} |\n"
        f"| Objective value | ${r.total_objective:,.0f} |"
    )


def _objective_decomposition(diagnostics: "ScenarioDiagnostics") -> str:
    d = diagnostics.winner_decomposition()
    return (
        "## Objective Decomposition\n\n"
        "| Component | Amount |\n"
        "|-----------|--------|\n"
        f"| Loan interest income | ${d.loan_interest_income:,.0f} |\n"
        f"| Expected credit loss | (${d.expected_credit_loss:,.0f}) |\n"
        f"| Loan income after losses | ${d.loan_income_after_losses:,.0f} |\n"
        f"| Deposit interest cost | (${d.deposit_interest_cost:,.0f}) |\n"
        f"| Wholesale funding cost | (${d.wholesale_funding_cost:,.0f}) |\n"
        f"| Excess deposit income | ${d.excess_deposit_income:,.0f} |\n"
        f"| Funding shortfall penalty | (${d.funding_shortfall_penalty:,.0f}) |\n"
        f"| **Total objective** | **${d.total_objective:,.0f}** |"
    )


def _funding_position(diagnostics: "ScenarioDiagnostics") -> str:
    r = diagnostics.best_result
    return (
        "## Expected Volumes and Funding Position\n\n"
        "| Item | Amount |\n"
        "|------|--------|\n"
        f"| Expected loan volume | ${r.expected_loan_volume:,.0f} |\n"
        f"| Expected deposit volume | ${r.expected_deposit_volume:,.0f} |\n"
        f"| Funding shortfall | ${r.funding_shortfall:,.0f} |"
    )


def _sensitivity_section(sensitivity: Optional[pd.DataFrame]) -> str:
    if sensitivity is None or sensitivity.empty:
        return "## Sensitivity Summary\n\nSensitivity analysis not provided for this run."
    cols = [
        "scenario_name",
        "best_loan_rate",
        "best_deposit_rate",
        "objective_value",
        "delta_objective_pct",
        "boundary_warning_flag",
    ]
    available = [c for c in cols if c in sensitivity.columns]
    sub = sensitivity[available].copy()
    if "best_loan_rate" in sub:
        sub["best_loan_rate"] = sub["best_loan_rate"].map("{:.2%}".format)
    if "best_deposit_rate" in sub:
        sub["best_deposit_rate"] = sub["best_deposit_rate"].map("{:.2%}".format)
    if "objective_value" in sub:
        sub["objective_value"] = sub["objective_value"].map("${:,.0f}".format)
    if "delta_objective_pct" in sub:
        sub["delta_objective_pct"] = sub["delta_objective_pct"].map("{:+.1%}".format)
    if "boundary_warning_flag" in sub:
        sub["boundary_warning_flag"] = sub["boundary_warning_flag"].map(
            lambda value: "Yes" if value else "No"
        )
    table = _markdown_table(sub)
    return f"## Sensitivity Summary\n\n{table}"


def _markdown_table(frame: pd.DataFrame) -> str:
    lines = [
        "| " + " | ".join(frame.columns) + " |",
        "| " + " | ".join(["---"] * len(frame.columns)) + " |",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _boundary_section(diagnostics: "ScenarioDiagnostics") -> str:
    boundary = diagnostics.boundary_diagnostics
    initial = diagnostics.initial_boundary_diagnostics
    if boundary.has_warning:
        warnings_text = "\n".join(f"- WARNING: {w}" for w in boundary.warnings)
    else:
        warnings_text = "No boundary warnings — result is interior to searched grid."
    return (
        "## Boundary Diagnostics\n\n"
        f"{warnings_text}\n\n"
        f"- Initial boundary warning: {'Yes' if initial.has_warning else 'No'}\n"
        f"- Expansion rounds completed: {diagnostics.expansion_rounds}\n"
        f"- Post-expansion boundary warning: {'Yes' if boundary.has_warning else 'No'}\n"
        f"- Policy band breached: {'Yes' if diagnostics.policy_band_breached else 'No'}"
    )


def _model_risk_notes() -> str:
    return (
        "## Model-Risk Notes\n\n"
        "- Response functions are deterministic synthetic elasticity models. "
        "No calibration to empirical data has been performed.\n"
        "- ML diagnostic models train on synthetic outcomes from the same response functions "
        "used in optimization (self-consistency bias). They do not validate real customer behavior.\n"
        "- Elasticity parameters are point estimates with no confidence bounds or priors.\n"
        "- Wholesale funding cost and funding shortfall penalty are intentionally additive in this "
        "model (see `docs/model_governance.md` for the canonical funding economics assumption).\n"
        "- Portfolio-level pricing does not account for customer-level fairness or conduct obligations."
    )


def _committee_questions(config: "EngineConfig", diagnostics: "ScenarioDiagnostics") -> str:
    r = config.response
    m = config.market
    bs = config.balance_sheet
    boundary_q = (
        "The final result still sits on a grid boundary after adaptive expansion. "
        "Has the grid range, elasticity, and funding penalty been reviewed?"
        if diagnostics.boundary_diagnostics.has_warning
        else "Boundary checks passed — confirm the searched grid range is policy-appropriate."
    )
    return (
        "## Questions for Pricing Committee\n\n"
        f"1. Are the elasticity assumptions (loan rate: {r.loan_rate_elasticity:.0f}, "
        f"deposit rate: {r.deposit_rate_elasticity:.0f}, "
        f"loan competitor gap: {r.loan_competitor_gap_elasticity:.0f}, "
        f"deposit competitor gap: {r.deposit_competitor_gap_elasticity:.0f}) "
        "aligned with current market intelligence?\n"
        f"2. Is the wholesale funding spread of {m.wholesale_funding_spread:.2%} current?\n"
        f"3. Are the competitor rates (loan: {m.competitor_loan_rate:.2%}, "
        f"deposit: {m.competitor_deposit_rate:.2%}) up to date?\n"
        f"4. {boundary_q}\n"
        f"5. Are the funding assumptions (required ratio: {bs.required_deposit_funding_ratio:.0%}, "
        f"penalty rate: {bs.funding_shortfall_penalty_rate:.2%}) aligned with current risk appetite?"
    )


def _synthetic_data_limitation() -> str:
    return (
        "## Synthetic Data Limitation\n\n"
        "All customer populations and response outcomes are generated from synthetic assumptions. "
        "This engine is a decision-support prototype. Results must not be treated as production "
        "pricing decisions without governed calibration to empirical data, regulatory review, "
        "and human approval."
    )
