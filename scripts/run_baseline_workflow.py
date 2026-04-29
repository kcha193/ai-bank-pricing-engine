"""Run the baseline pricing workflow from the command line."""

from __future__ import annotations

from pathlib import Path

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.reporting import fmt_bps, fmt_money, fmt_rate
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def main() -> None:
    """Run the baseline scenario and print the same sections as the walkthrough."""
    config = load_config(Path("configs/base.yaml"))
    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    print("Best joint pricing decision")
    print(f"  Home loan rate:          {fmt_rate(result.loan_rate)}")
    print(f"  Term deposit rate:       {fmt_rate(result.deposit_rate)}")
    print(f"  Objective value:         {fmt_money(result.objective_value)}")
    print(f"  Loan income after loss:  {fmt_money(result.loan_income_after_losses)}")
    print(f"  Deposit cost:            {fmt_money(result.deposit_cost)}")
    print(f"  Wholesale funding cost:  {fmt_money(result.wholesale_funding_cost)}")
    print(f"  Funding penalty:         {fmt_money(result.funding_penalty)}")
    print(f"  Expected loan volume:    {fmt_money(result.expected_loan_volume)}")
    print(f"  Expected deposit volume: {fmt_money(result.expected_deposit_volume)}")
    print(f"  Funding shortfall:       {fmt_money(result.funding_shortfall)}")
    print()

    print("Market-Relative Pricing Recommendation")
    print(f"  Market home loan reference rate:       {fmt_rate(config.market.home_loan_reference_rate)}")
    print(f"  Recommended home loan rate:            {fmt_rate(result.loan_rate)}")
    print(f"  Home loan spread to market:            {fmt_bps(result.home_loan_spread_to_market_bps)}")
    print(
        "  Market term deposit reference rate:    "
        f"{fmt_rate(config.market.term_deposit_reference_rate)}"
    )
    print(f"  Recommended term deposit rate:         {fmt_rate(result.deposit_rate)}")
    print(
        "  Term deposit spread to market:         "
        f"{fmt_bps(result.term_deposit_spread_to_market_bps)}"
    )
    print()

    print("Top 5 pricing candidates")
    for rank, row in diagnostics.top_results(5).iterrows():
        print(
            f"  {rank + 1}. loan={fmt_rate(row.loan_rate)}, "
            f"deposit={fmt_rate(row.deposit_rate)}, "
            f"objective={fmt_money(row.total_objective)}, "
            f"loans={fmt_money(row.expected_loan_volume)}, "
            f"deposits={fmt_money(row.expected_deposit_volume)}, "
            f"shortfall={fmt_money(row.funding_shortfall)}"
        )
    print()

    print("Winner objective decomposition")
    decomposition = diagnostics.winner_decomposition()
    print(f"  Loan interest income:      {fmt_money(decomposition.loan_interest_income)}")
    print(f"  Expected credit loss:      {fmt_money(decomposition.expected_credit_loss)}")
    print(f"  Deposit interest cost:     {fmt_money(decomposition.deposit_interest_cost)}")
    print(f"  Wholesale funding cost:    {fmt_money(decomposition.wholesale_funding_cost)}")
    print(f"  Funding shortfall penalty: {fmt_money(decomposition.funding_shortfall_penalty)}")
    print(f"  Total objective:           {fmt_money(decomposition.total_objective)}")
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
            f"loan={fmt_rate(row.best_loan_rate)}, "
            f"deposit={fmt_rate(row.best_deposit_rate)}, "
            f"objective={fmt_money(row.objective_value)}, "
            f"loans={fmt_money(row.expected_loan_volume)}, "
            f"deposits={fmt_money(row.expected_deposit_volume)}, "
            f"shortfall={fmt_money(row.funding_shortfall)}, "
            f"boundary_warning={warning}"
        )


if __name__ == "__main__":
    main()
