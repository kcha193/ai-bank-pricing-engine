"""Run the baseline joint pricing scenario."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def main() -> None:
    config = load_config(Path("configs/base.yaml"))
    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    print("Best joint pricing decision")
    print(f"  Home loan rate:          {result.loan_rate:.2%}")
    print(f"  Term deposit rate:       {result.deposit_rate:.2%}")
    print(f"  Objective value:         ${result.objective_value:,.0f}")
    print(f"  Loan income after loss:  ${result.loan_income_after_losses:,.0f}")
    print(f"  Deposit cost:            ${result.deposit_cost:,.0f}")
    print(f"  Wholesale funding cost:  ${result.wholesale_funding_cost:,.0f}")
    print(f"  Funding penalty:         ${result.funding_penalty:,.0f}")
    print(f"  Expected loan volume:    ${result.expected_loan_volume:,.0f}")
    print(f"  Expected deposit volume: ${result.expected_deposit_volume:,.0f}")
    print(f"  Funding shortfall:       ${result.funding_shortfall:,.0f}")
    print()
    print("Top 5 pricing candidates")
    for rank, row in diagnostics.top_results(5).iterrows():
        print(
            f"  {rank + 1}. loan={row.loan_rate:.2%}, "
            f"deposit={row.deposit_rate:.2%}, "
            f"objective=${row.total_objective:,.0f}, "
            f"loans=${row.expected_loan_volume:,.0f}, "
            f"deposits=${row.expected_deposit_volume:,.0f}, "
            f"shortfall=${row.funding_shortfall:,.0f}"
        )
    print()
    print("Winner objective decomposition")
    decomposition = diagnostics.winner_decomposition()
    print(f"  Loan interest income:     ${decomposition.loan_interest_income:,.0f}")
    print(f"  Expected credit loss:     ${decomposition.expected_credit_loss:,.0f}")
    print(f"  Deposit interest cost:    ${decomposition.deposit_interest_cost:,.0f}")
    print(f"  Wholesale funding cost:   ${decomposition.wholesale_funding_cost:,.0f}")
    print(f"  Funding shortfall penalty: ${decomposition.funding_shortfall_penalty:,.0f}")
    print(f"  Total objective:          ${decomposition.total_objective:,.0f}")
    print()
    print("Boundary diagnostics")
    if diagnostics.boundary_diagnostics.has_warning:
        for warning in diagnostics.boundary_diagnostics.warnings:
            print(f"  WARNING: {warning}")
    else:
        print("  No boundary warnings.")
    print()
    print("Sensitivity summary")
    sensitivity = run_sensitivity_analysis(config)
    for row in sensitivity.itertuples(index=False):
        warning = "yes" if row.boundary_warning_flag else "no"
        print(
            f"  {row.scenario_name}: "
            f"loan={row.best_loan_rate:.2%}, "
            f"deposit={row.best_deposit_rate:.2%}, "
            f"objective=${row.objective_value:,.0f}, "
            f"loans=${row.expected_loan_volume:,.0f}, "
            f"deposits=${row.expected_deposit_volume:,.0f}, "
            f"shortfall=${row.funding_shortfall:,.0f}, "
            f"boundary_warning={warning}"
        )


if __name__ == "__main__":
    main()
